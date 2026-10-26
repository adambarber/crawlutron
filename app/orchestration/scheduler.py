from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from secrets import SystemRandom
from typing import TYPE_CHECKING, Protocol, TypeVar

from app.orchestration.scheduler_exceptions import CircuitBreakerOpen
from app.vocabulary.taxonomy import DOMAIN_HEALTH_BY_STATUS, ResultStatus

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings

logger = logging.getLogger(__name__)

# Crawl-delay jitter draws from OS entropy, not Mersenne Twister: a predictable
# per-domain delay pattern is a fingerprint, and the syscall is free here.
_JITTER = SystemRandom()

# The jitter band. A fixed delay would make a fleet retry a host in lockstep; the
# band spreads the retries without changing the configured mean.
_JITTER_LOW, _JITTER_HIGH = 0.8, 1.2

#: How long a successful strategy pin stays authoritative before the domain
#: re-evaluates the whole ladder (#303); a browser-only page cannot pin forever.
_STRATEGY_PIN_TTL_SECONDS = 300.0


def jittered_delay(base_delay: float) -> float:
    """``base_delay`` with the crawl-delay jitter applied, or 0 for a zero base.
    Module-level so the jitter band lives in exactly one place, tested directly
    rather than re-derived out of ``run`` by tests that assert the re-derivation.
    """
    if base_delay <= 0:
        return 0.0
    return base_delay * _JITTER.uniform(_JITTER_LOW, _JITTER_HIGH)


class ScheduledJob(Protocol):
    """The structural contract ``DomainScheduler.run`` actually depends on: the crawl
    and render pipelines submit different job shapes, and the reporting URL is
    deliberately not a member (the shapes name it differently). Members are read-only properties.
    """

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
    ``.elapsed_ms``. A protocol (rather than naming either pipeline's type) keeps
    ``run`` generic, and ``elapsed_ms`` is declared so a shape without it isn't silently skipped.
    """

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


class TokenBucket:
    """Token bucket rate limiter: refills at ``rate`` tokens/sec, caps at ``capacity``."""

    def __init__(self, rate: float, capacity: int = 5) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def consume(self, tokens: float = 1.0) -> float:
        """Reserve ``tokens``, returning seconds to wait before firing (0 if available).

        A short caller drives the balance negative, so overlapping callers queue
        behind what was taken (the max_rps herd); rate >= 1/MAX_RATE_LIMIT_RPS.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now
        self.tokens -= tokens
        return max(0.0, -self.tokens / self.rate)


# Upper bound on how long any single job will sleep on a domain cooldown.
MAX_COOLDOWN_WAIT = 300.0

# Purge the cooldown/circuit mappings at most once per this many sets, so a
# 429-heavy crawl does not rebuild both dicts per result. ``run`` purges up front.
COOLDOWN_PURGE_INTERVAL = 100


class DomainScheduler:
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._global_limit = max(1, global_limit)
        self._per_domain_limit = max(1, per_domain_limit)
        self._delay_seconds = max(0.0, delay_seconds)
        # Per-domain minimum delay (e.g. robots Crawl-delay); the host's floor is always respected.
        self._domain_min_delay: dict[str, float] = defaultdict(float)
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

        # Track when domains are allowed to be hit again to implement domain-level cooldowns
        self._domain_cooldowns: dict[str, float] = {}

        # Consecutive circuit-breaker trips per domain (drives exponential backoff).
        self._circuit_trip_count: dict[str, int] = defaultdict(int)

        # Token buckets per domain for RPS rate limiting
        self._token_buckets: dict[str, TokenBucket] = {}

        # Circuit breaker: consecutive failures per domain
        self._domain_failures: dict[str, int] = defaultdict(int)
        self._circuit_open_until: dict[str, float] = {}

        # Adaptive throttling state: EMA of observed latency (ms) per domain.
        self._domain_latency_ema: dict[str, float] = {}

        # Per-job strategy pinning: once a domain proves it needs a strategy,
        # later fetches in the same job start there, not from the cheapest rung.
        self._domain_strategy_pins: dict[str, tuple[str, float]] = {}

        # Purging both dicts per 429 made heavy crawls quadratic; the purge is
        # amortized to one per interval (``run`` still purges up front).
        self._sets_since_purge = 0

    def set_domain_cooldown(self, domain: str, seconds: float) -> None:
        """Set a cooldown period for a domain after a rate limit (429) response.
        Also purges elapsed cooldowns/circuits so the mappings cannot leak memory,
        on an amortized cadence -- rebuilding both dicts per 429 is quadratic.
        """
        if seconds > 0:
            self._domain_cooldowns[domain] = time.time() + seconds
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
        self._domain_failures[domain] = 0
        self._circuit_trip_count[domain] = 0

    def record_failure(self, domain: str) -> None:
        """Increment the failure counter; escalate the circuit-breaker cooldown
        exponentially with each threshold-crossing batch of failures."""
        self._domain_failures[domain] += 1
        # Each ``threshold`` failures escalates one step (base, base*2, ...,
        # capped); forty consecutive failures park a domain for the max cooldown.
        n = self._domain_failures[domain] // self.circuit_threshold
        if n >= 1 and n > self._circuit_trip_count.get(domain, 0):
            self._circuit_trip_count[domain] = n
            cooldown = min(
                self._circuit_cooldown_base * (2 ** (n - 1)),
                self._circuit_cooldown_max,
            )
            self._circuit_open_until[domain] = time.monotonic() + cooldown

    def is_circuit_open(self, domain: str) -> bool:
        """Check if the circuit breaker is open for a domain."""
        expiry = self._circuit_open_until.get(domain)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            # Cooldown elapsed (half-open): reset counters so a recovered domain
            # is not penalised by a stale escalation.
            del self._circuit_open_until[domain]
            self._domain_failures[domain] = 0
            self._circuit_trip_count[domain] = 0
            return False
        return True

    def record_latency(self, domain: str, latency_ms: float) -> None:
        """Feed observed response latency (ms) into the adaptive delay estimator.
        Negative is the only rejected value -- it is a bad measurement, not a fast
        response; there is no ``None`` case, the caller decides when nothing was measured.
        """
        if latency_ms < 0:
            return
        prev = self._domain_latency_ema.get(domain)
        alpha = self._adaptive_smoothing
        self._domain_latency_ema[domain] = (
            latency_ms if prev is None else prev * (1.0 - alpha) + latency_ms * alpha
        )

    def get_pinned_strategy(self, domain: str) -> str | None:
        """Return the strategy pinned for ``domain`` within this job, or None.
        An expired pin re-evaluates: it is dropped and the domain re-climbs.
        """
        pin = self._domain_strategy_pins.get(domain)
        if pin is None:
            return None
        strategy, pinned_at = pin
        if self._clock() - pinned_at > _STRATEGY_PIN_TTL_SECONDS:
            self._domain_strategy_pins.pop(domain, None)
            return None
        return strategy

    def set_pinned_strategy(self, domain: str, strategy: str) -> None:
        """Record the strategy that succeeded for ``domain`` so future fetches
        in this job start at this strategy instead of re-climbing the ladder.
        """
        self._domain_strategy_pins[domain] = (strategy, self._clock())

    def clear_pinned_strategy(self, domain: str) -> None:
        """Drop the pin for ``domain``; used when the pinned strategy is no longer
        viable (e.g. the browser pool is exhausted) so the fallback is tried (#303).
        """
        self._domain_strategy_pins.pop(domain, None)

    def _recompute_delay(self, domain: str) -> float:
        """Target per-domain delay (seconds) from the latency EMA."""
        lat = self._domain_latency_ema.get(domain)
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
        return max(base, self._domain_min_delay.get(domain, 0.0))

    def set_domain_min_delay(self, domain: str, seconds: float) -> None:
        """Enforce a minimum inter-request delay for a domain (robots Crawl-delay).
        Non-finite values are ignored and finite ones clamped to the politeness
        ceiling, so a hostile robots.txt cannot install an unbounded per-domain floor.
        """
        if seconds and seconds > 0 and math.isfinite(seconds):
            clamped = min(float(seconds), MAX_COOLDOWN_WAIT)
            self._domain_min_delay[domain] = max(self._domain_min_delay.get(domain, 0.0), clamped)

    def purge_expired(self) -> None:
        """Drop cooldown and circuit-breaker entries that have already elapsed."""
        now = time.time()
        self._domain_cooldowns = {d: exp for d, exp in self._domain_cooldowns.items() if exp > now}
        now_mono = time.monotonic()
        self._circuit_open_until = {d: exp for d, exp in self._circuit_open_until.items() if exp > now_mono}
        pin_now = self._clock()
        self._domain_strategy_pins = {
            d: (strat, at)
            for d, (strat, at) in self._domain_strategy_pins.items()
            if pin_now - at <= _STRATEGY_PIN_TTL_SECONDS
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
        # Purge stale state up front so dicts cannot grow without bound.
        self.purge_expired()
        global_gate = asyncio.Semaphore(self._global_limit)
        domain_gates: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_domain_limit),
        )
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
            cooldown_expiry = self._domain_cooldowns.get(job.domain)
            if cooldown_expiry is not None:
                now = time.time()
                if now < cooldown_expiry:
                    # Cap the wait: a hostile Retry-After must not park this
                    # gather for years; beyond the cap the origin re-asserts.
                    await asyncio.sleep(min(cooldown_expiry - now, MAX_COOLDOWN_WAIT))
            if await self._run_one_gated(job, domain_gates, global_gate, deliver, worker, failed_result):
                return

        # return_exceptions=True: one job's failure must not cancel the others;
        # the failing job is simply absent from the returned mapping.
        await asyncio.gather(*(run_one(job) for job in jobs), return_exceptions=True)
        if on_result is not None:
            return None
        return results

    async def _run_one_gated(
        self,
        job: _Job,
        domain_gates: dict[str, asyncio.Semaphore],
        global_gate: asyncio.Semaphore,
        deliver: Callable[[int, _Result], Awaitable[None]],
        worker: SchedulerWorker[_Job, _Result],
        failed_result: Callable[[_Job, BaseException], _Result],
    ) -> bool:
        """Breaker skip (True = handled) or the gated worker run; False = done."""
        # Circuit breaker: if open, PARK the batch (cooldown path) instead of
        # re-dispatching into the breaker; past the cap the envelope is terminal.
        if self.is_circuit_open(job.domain):
            await self._park_open_breaker(job.domain)
            if self.is_circuit_open(job.domain):
                # In on_result mode the failed envelope must take the same channel:
                # a direct dict write once raised, was swallowed, and the job vanished.
                await deliver(
                    job.index,
                    failed_result(job, CircuitBreakerOpen(self._domain_cooldowns.get(job.domain))),
                )
                return True

        # Token bucket rate limiting.
        if self._max_rps > 0:
            bucket = self._token_buckets.get(job.domain)
            if bucket is None:
                bucket = TokenBucket(rate=self._max_rps)
                self._token_buckets[job.domain] = bucket
            wait_seconds = bucket.consume(1.0)
            if wait_seconds > 0:
                # The reserve is taken; this sleep only spaces this job's firing,
                # and the next caller's consume sees the negative balance.
                await asyncio.sleep(wait_seconds)

        async with domain_gates[job.domain]:
            # Jitter sleeps while holding ONLY the per-domain gate: sleeping
            # inside global_gate would burn a global slot on pure waiting.
            base_delay = self.current_delay(job.domain)
            delay_sleep = jittered_delay(base_delay)
            await asyncio.sleep(delay_sleep)
            async with global_gate:
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

    async def _park_open_breaker(self, domain: str) -> None:
        """Record the open window as a domain cooldown and sleep it out, capped.
        The same park a 429 takes; the caller re-checks the breaker after it."""
        expiry = self._circuit_open_until.get(domain)
        remaining = 0.0 if expiry is None else max(0.0, expiry - time.monotonic())
        if remaining > 0:
            self.set_domain_cooldown(domain, remaining)
            await asyncio.sleep(min(remaining, MAX_COOLDOWN_WAIT))

    @property
    def domain_cooldowns(self) -> dict[str, float]:
        return dict(self._domain_cooldowns)
