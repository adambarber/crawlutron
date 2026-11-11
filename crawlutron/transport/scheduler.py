from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, TypeVar

from crawlutron.transport.scheduler_domain_state import DomainGate, DomainState
from crawlutron.transport.scheduler_exceptions import CircuitBreakerOpen
from crawlutron.transport.scheduler_pacing import (
    COOLDOWN_PURGE_INTERVAL,
    MAX_COOLDOWN_WAIT,
    TokenBucket,
    jittered_delay,
    sleep_out,
)
from crawlutron.vocabulary.taxonomy import DOMAIN_HEALTH_BY_STATUS, ResultStatus

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings

logger = logging.getLogger(__name__)

#: How long a successful strategy pin stays authoritative before the domain
#: re-evaluates the whole ladder; a browser-only page cannot pin forever.
_STRATEGY_PIN_TTL_SECONDS = 300.0


class ScheduledJob(Protocol):
    """The structural contract ``DomainScheduler.run`` depends on: the crawl and
    render pipelines submit different job shapes, and the reporting URL is
    deliberately not a member. Members are read-only properties."""

    @property
    def index(self) -> int:
        """Submission order; the key the caller aligns results against."""
        ...

    @property
    def domain(self) -> str:
        """Registrable domain pacing/breakers are scoped to."""
        ...


_Job = TypeVar("_Job", bound=ScheduledJob)


class SchedulerResult(Protocol):
    """What the domain scheduler reads off a worker's result: ``.status`` and
    ``.elapsed_ms``. A protocol keeps ``run`` generic, and ``elapsed_ms`` is
    declared so a shape without it isn't silently skipped."""

    @property
    def status(self) -> ResultStatus:
        """Terminal status of this job's attempt."""
        ...

    @property
    def elapsed_ms(self) -> int:
        """Milliseconds this attempt took, or 0 when nothing observed it."""
        ...


_Result = TypeVar("_Result", bound=SchedulerResult)
# The worker is parameterized on the job it accepts; the alias uses a bound
# TypeVar because mypy cannot solve a contravariant one from a bare alias.
SchedulerWorker = Callable[[_Job], Awaitable[_Result]]


def _circuit_remaining(scheduler: DomainScheduler, domain: str) -> float:
    """Seconds left on the domain's open breaker window, 0 when closed/half-open."""
    return scheduler._state(domain).breaker_remaining(now=scheduler._clock())


async def _park_open_breaker(scheduler: DomainScheduler, domain: str) -> None:
    """Record the open window as a capped cooldown and sleep the same capped
    number: a raw-window stamp would outlive half-open, napping batches past it."""
    remaining = _circuit_remaining(scheduler, domain)
    if remaining > 0:
        scheduler.set_domain_cooldown(domain, min(remaining, MAX_COOLDOWN_WAIT))
        await asyncio.sleep(min(remaining, MAX_COOLDOWN_WAIT))


async def _skip_if_breaker_open[Job: ScheduledJob, Result: SchedulerResult](
    scheduler: DomainScheduler,
    job: Job,
    deliver: Callable[[int, Result], Awaitable[None]],
    failed_result: Callable[[Job, BaseException], Result],
) -> bool:
    """Park an open breaker (cooldown path), report whether the job is handled;
    past the cap the envelope is terminal. Shared by the pre-run check and the
    post-nap re-check, so a nap cannot skip a peer's trip.
    """
    if not scheduler.is_circuit_open(job.domain):
        return False
    await _park_open_breaker(scheduler, job.domain)
    if scheduler.is_circuit_open(job.domain):
        # In on_result mode the failed envelope must take the same channel: a
        # direct dict write once raised, was swallowed, and the job vanished.
        remaining = round(_circuit_remaining(scheduler, job.domain))
        skip = CircuitBreakerOpen(f"{job.domain} circuit open: {remaining}s of cooldown remain")
        await deliver(job.index, failed_result(job, skip))
        return True
    return False


class DomainScheduler:
    """Per-instance domain pacing/breakers/rate accounting; nothing shared.

    Every per-domain record is one :class:`DomainState` in one map: cooldowns,
    breaker, politeness floor, fire-time reservation, EMA, bucket, gate."""

    @classmethod
    def from_settings(
        cls,
        settings: CrawlerSettings,
        *,
        global_limit: int,
        max_rps: float,
    ) -> DomainScheduler:
        """The single construction path: every knob reads its settings home,
        never re-defaulted here; coercions are part of the contract."""
        return cls(
            global_limit=max(1, int(global_limit)),
            per_domain_limit=max(1, int(settings.per_domain_concurrency)),
            delay_seconds=max(0.0, float(settings.per_domain_delay_ms) / 1000),
            max_rps=max(0.0, float(max_rps)),
            adaptive=bool(settings.adaptive_throttling),
            adaptive_min_delay=float(settings.adaptive_min_delay),
            adaptive_max_delay=float(settings.adaptive_max_delay),
            adaptive_latency_factor=float(settings.adaptive_latency_factor),
        )

    def __init__(
        self,
        *,
        global_limit: int,
        per_domain_limit: int,
        delay_seconds: float,
        max_rps: float = 0.0,
        circuit_threshold: int = 5,
        circuit_cooldown: float = 600.0,
        circuit_cooldown_base: float = 30.0,
        adaptive: bool = True,
        adaptive_min_delay: float = 0.5,
        adaptive_max_delay: float = 30.0,
        adaptive_latency_factor: float = 1.0,
        adaptive_smoothing: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # THE one clock: every timestamp here flows through it, monotonic --
        # an in-memory timeline no wall step can bend (#580).
        self._clock = clock
        self._global_limit = max(1, global_limit)
        self._per_domain_limit = max(1, per_domain_limit)
        self._delay_seconds = max(0.0, delay_seconds)
        self._max_rps = max(0.0, max_rps)
        self.circuit_threshold = max(1, circuit_threshold)
        # Exponential breaker cooldown: first trip waits ``base``, each
        # consecutive trip doubles (capped) -- a tarpit parks progressively longer.
        self._circuit_cooldown_base = max(1.0, circuit_cooldown_base)
        self._circuit_cooldown_max = max(self._circuit_cooldown_base, circuit_cooldown)
        # Adaptive throttling (AutoThrottle-style): per-domain delay is driven by
        # observed response latency rather than a single fixed value.
        self._adaptive = adaptive
        self._adaptive_min_delay = max(0.0, adaptive_min_delay)
        self._adaptive_max_delay = max(self._adaptive_min_delay, adaptive_max_delay)
        self._adaptive_latency_factor = max(0.0, adaptive_latency_factor)
        self._adaptive_smoothing = min(1.0, max(0.0, adaptive_smoothing))

        # The one per-domain map: nine parallel dicts collapsed into records.
        self._domain_state: dict[str, DomainState] = {}

        # Amortized purge: per-429 purging made heavy crawls quadratic.
        self._sets_since_purge = 0

    def _state(self, domain: str) -> DomainState:
        """The domain's record, minted on first sight with this scheduler's knobs."""
        state = self._domain_state.get(domain)
        if state is None:
            state = DomainState()
            state.gate = DomainGate(self._per_domain_limit)
            self._domain_state[domain] = state
        return state

    def set_domain_cooldown(self, domain: str, seconds: float) -> None:
        """Extend a domain's cooldown to the LONGER of its standing debt and
        ``seconds``: debts only grow -- a breaker park may never truncate a
        longer Retry-After window. Also purges on the amortized cadence."""
        now = self._clock()
        if seconds > 0:
            state = self._state(domain)
            state.cooldown_until = max(state.cooldown_until or 0.0, now + seconds)
        self._sets_since_purge += 1
        if self._sets_since_purge >= COOLDOWN_PURGE_INTERVAL:
            self._sets_since_purge = 0
            self.purge_expired()

    def _record_result_health(self, domain: str, result: SchedulerResult) -> None:
        """Apply a result's domain-health verdict. See DOMAIN_HEALTH_BY_STATUS.
        One decision, kept out of ``run_one``'s ``async with`` so the loop stays
        readable; the verdict mapping is the whole question of a status's health.
        """
        accounting = DOMAIN_HEALTH_BY_STATUS.get(result.status, "none")
        if accounting == "failure":
            self.record_failure(domain)
            return
        if accounting != "success":
            return
        self.record_success(domain)
        # Feed observed latency to the adaptive estimator; one declared member
        # satisfied by both result shapes reads the same on either pipeline.
        latency = result.elapsed_ms
        if latency:
            self.record_latency(domain, latency)

    def record_success(self, domain: str) -> None:
        """Reset the failure counter (and exponential backoff) for a domain."""
        self._state(domain).reset_breaker()

    def record_failure(self, domain: str) -> None:
        """Charge one failure against the domain's breaker (escalation math
        lives with the counters it escalates, in DomainState)."""
        self._state(domain).charge_failure(
            now=self._clock(),
            threshold=self.circuit_threshold,
            base=self._circuit_cooldown_base,
            cooldown_max=self._circuit_cooldown_max,
        )

    def is_circuit_open(self, domain: str) -> bool:
        """Check if the circuit breaker is open for a domain."""
        return self._state(domain).breaker_open(now=self._clock())

    def record_latency(self, domain: str, latency_ms: float) -> None:
        """Feed observed response latency (ms) into the adaptive delay estimator.
        Negative is rejected: a bad measurement, not a fast response."""
        if latency_ms < 0:
            return
        self._state(domain).feed_latency(
            latency_ms, smoothing=self._adaptive_smoothing, clock_now=self._clock()
        )

    def get_pinned_strategy(self, domain: str) -> str | None:
        """The strategy pinned for ``domain`` within this job, or None. An expired
        pin re-evaluates: it is dropped and the domain re-climbs."""
        return self._state(domain).get_pin(clock_now=self._clock(), ttl=_STRATEGY_PIN_TTL_SECONDS)

    def set_pinned_strategy(self, domain: str, strategy: str) -> None:
        """Record the strategy that succeeded for ``domain`` so future fetches in
        this job start at this strategy instead of re-climbing the ladder."""
        self._state(domain).set_pin(strategy, clock_now=self._clock())

    def clear_pinned_strategy(self, domain: str) -> None:
        """Drop the pin for ``domain``; used when the pinned strategy is no longer
        viable (e.g. the browser pool is exhausted) so the fallback is tried."""
        self._state(domain).pin = None

    def _recompute_delay(self, domain: str) -> float:
        """Target per-domain delay (seconds) from the latency EMA."""
        lat = self._state(domain).ema
        if lat is None:
            return self._delay_seconds
        target = (lat / 1000.0) * self._adaptive_latency_factor
        return min(self._adaptive_max_delay, max(self._adaptive_min_delay, target))

    def current_delay(self, domain: str) -> float:
        """Delay (seconds) to apply before the next request to ``domain``.
        Latency-driven when adaptive throttling is on, else the fixed ``delay_seconds``;
        a per-domain robots Crawl-delay floor (set_domain_min_delay) is always respected.
        """
        base = self._delay_seconds if not self._adaptive else self._recompute_delay(domain)
        return max(base, self._state(domain).min_delay)

    def set_domain_min_delay(self, domain: str, seconds: float) -> None:
        """Enforce a minimum inter-request delay for a domain (robots Crawl-delay).
        Non-finite values are ignored and finite ones clamped to the politeness
        ceiling, so a hostile robots.txt cannot install an unbounded per-domain floor.
        """
        if seconds and seconds > 0 and math.isfinite(seconds):
            clamped = min(float(seconds), MAX_COOLDOWN_WAIT)
            state = self._state(domain)
            state.min_delay = max(state.min_delay, clamped)

    def purge_expired(self) -> None:
        """Drop every record whose fields are all inert; liveness is per-field
        inside :class:`DomainState`, so one predicate owns the whole question."""
        now = self._clock()
        self._domain_state = {
            domain: state
            for domain, state in self._domain_state.items()
            if state.live(
                now=now,
                window=self._circuit_cooldown_max,
                pin_ttl=_STRATEGY_PIN_TTL_SECONDS,
            )
        }

    async def run(
        self,
        jobs: list[_Job],
        worker: SchedulerWorker[_Job, _Result],
        on_result: Callable[[int, _Result], Awaitable[None]] | None = None,
        *,
        failed_result: Callable[[_Job, BaseException], _Result],
    ) -> dict[int, _Result] | None:
        """Run ``jobs`` through ``worker``: when ``on_result`` is set each result is
        delivered and NOT retained (peak RAM tracks the in-flight set) and ``run``
        returns None; else it returns a ``job.index -> result`` mapping (raised jobs absent).
        """
        # Purge stale records up front so the map cannot grow without bound.
        self.purge_expired()
        # Named for its scope: this semaphore is one batch's ceiling, not the
        # scheduler's lifetime gate.
        batch_gate = asyncio.Semaphore(self._global_limit)
        # Retain results in memory only when the caller needs them back;
        # with on_result set they spill per-job and never accumulate here.
        results: dict[int, _Result] = {}

        # on_result and the mapping are exclusive channels; one sink keeps them
        # from drifting. A sink failure is logged+swallowed -- never a page verdict.
        async def deliver(index: int, result: _Result) -> None:
            if on_result is None:
                results[index] = result
                return
            try:
                await on_result(index, result)
            except Exception:
                logger.exception(
                    "on_result_failed index=%s status=%s", index, getattr(result, "status", None)
                )

        async def run_one(job: _Job) -> None:
            # Domain cooldown BEFORE the global semaphore, or one rate-limited
            # domain blocks every other domain's slot (cooldown deadlock).
            await sleep_out(self._state(job.domain).cooldown_until, self._clock)
            if await self._run_one_gated(job, batch_gate, deliver, worker, failed_result):
                return

        # return_exceptions=True: one job's failure must not cancel the others;
        # the failing job is simply absent from the returned mapping.
        await asyncio.gather(*(run_one(job) for job in jobs), return_exceptions=True)
        if on_result is not None:
            return None
        return results

    def _reserve_fire(self, domain: str) -> float:
        """Reserve the domain's next fire slot; return seconds until it arrives.
        THE spacing mechanism: a fire time is booked under the domain gate, one
        per fire, so same-domain requests space by the delay. Monotonic."""
        state = self._state(domain)
        now = self._clock()
        fire_at = max(now, state.next_fire or 0.0)
        state.next_fire = fire_at + jittered_delay(self.current_delay(domain))
        return fire_at - now

    async def _fire_wait(self, job: ScheduledJob) -> None:
        """One sleep for both pacing debts: the bucket reserve (arrival-ordered)
        and the fire-time reservation, never summed -- politeness gaps do not
        double-count. Zero-delay domains reserve nothing and owe no sleep.
        """
        bucket_wait = 0.0
        if self._max_rps > 0:
            state = self._state(job.domain)
            if state.bucket is None:
                state.bucket = TokenBucket(rate=self._max_rps, clock=self._clock)
            bucket_wait = state.bucket.consume(1.0)
        # Reserve under the gate, sleep OUTSIDE it: no gate sees a sleep, and
        # per-domain slots stay live during the nap.
        async with self._state(job.domain).gate:
            reserve_wait = self._reserve_fire(job.domain)
        wait = max(bucket_wait, reserve_wait)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _run_one_gated(
        self,
        job: _Job,
        batch_gate: asyncio.Semaphore,
        deliver: Callable[[int, _Result], Awaitable[None]],
        worker: SchedulerWorker[_Job, _Result],
        failed_result: Callable[[_Job, BaseException], _Result],
    ) -> bool:
        """Breaker skip (True = handled) or the gated worker run; False = done."""
        # Circuit breaker FIRST: a parked job spends no tokens or reservations
        # -- the reserve never precedes the initial breaker check.
        if await _skip_if_breaker_open(self, job, deliver, failed_result):
            return True

        await self._fire_wait(job)

        # The nap must not skip backoff that fired while it slept: a peer's
        # breaker trip or 429 cooldown parks us here, outside every gate.
        if await _skip_if_breaker_open(self, job, deliver, failed_result):
            return True
        # A cooldown held this domain: sleep it out, then fire after the domain's
        # next slot -- not shoulder-to-shoulder with peers waking at the same expiry.
        expiry = self._state(job.domain).cooldown_until
        await sleep_out(expiry, self._clock)
        if expiry is not None:
            # Reservation only: the bucket reserve was already spent.
            async with self._state(job.domain).gate:
                reserve_wait = self._reserve_fire(job.domain)
            if reserve_wait > 0:
                await asyncio.sleep(reserve_wait)
        async with self._state(job.domain).gate:
            async with batch_gate:
                try:
                    result = await worker(job)
                    self._record_result_health(job.domain, result)
                except Exception as error:
                    self.record_failure(job.domain)
                    # Chokepoint for "a worker raised": the classified envelope
                    # is built here; ``failed_result`` keeps each pipeline's shape.
                    await deliver(job.index, failed_result(job, error))
                    return False
                # Delivery sits OUTSIDE the worker's try: a raising sink is
                # a delivery failure, not a charge against the domain's breaker.
                await deliver(job.index, result)
        return False

    @property
    def domain_cooldowns(self) -> dict[str, float]:
        return {d: s.cooldown_until for d, s in self._domain_state.items() if s.cooldown_until}

    def snapshot(self) -> dict[str, DomainState]:
        """An observation-only view: fresh DomainState copies per domain. Scalars are
        COPIED, so a reader can re-stamp without touching the scheduler's record;
        each DomainGate is SHARED -- a live semaphore, so occupancy stays truthful."""
        return {n: s.observation_copy() for n, s in self._domain_state.items()}

    @property
    def global_limit(self) -> int:
        """Configured global concurrency ceiling: the test read door (#633)."""
        return self._global_limit
