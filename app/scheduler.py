from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from secrets import SystemRandom
from typing import Protocol, TypeVar

from .crawl.scheduler_exceptions import _CircuitBreakerOpen
from .models import ResultStatus

logger = logging.getLogger(__name__)

# The crawl-delay jitter draws from the OS entropy pool rather than the
# Mersenne Twister. It is throughput shaping, not cryptography, so nothing here
# depends on reproducibility -- but a predictable per-domain delay pattern is a
# fingerprint, and the draw happens once per job against a network fetch, so the
# syscall costs nothing.
_JITTER = SystemRandom()

# The jitter band. A fixed delay would make a fleet retry a host in lockstep; the
# band spreads the retries without changing the configured mean.
_JITTER_LOW, _JITTER_HIGH = 0.8, 1.2


def jittered_delay(base_delay: float) -> float:
    """``base_delay`` with the crawl-delay jitter applied, or 0 for a zero base.

    Named and module-level so the band lives in exactly one place: the band used
    to be an inline expression inside ``run``, which is a loop that also holds
    the per-domain gate, so nothing could reach it in a test. A test that wanted
    to cover the band had to re-derive the expression locally, and a test that
    re-derives the thing it is testing asserts the re-derivation.
    """
    if base_delay <= 0:
        return 0.0
    return base_delay * _JITTER.uniform(_JITTER_LOW, _JITTER_HIGH)


class ScheduledJob(Protocol):
    """The structural contract ``DomainScheduler.run`` actually depends on.

    ``run`` is polymorphic over the job shape: the crawl pipeline submits
    ``CrawlTask`` (original_url + normalized_url) and the render pipeline
    submits ``CrawlJob`` (url). Declaring the parameter as ``CrawlJob`` hid
    that -- the scheduler then read ``job.url`` in the two error-envelope
    branches, which only exists on one of the two shapes, and the annotation
    was the only thing standing between the crawl path and an
    ``AttributeError`` inside a ``gather(return_exceptions=True)`` handler.

    The members are read-only properties on purpose: the scheduler never
    mutates a job, and a mutable-attribute protocol would reject the frozen
    dataclasses both shapes actually are. The reporting URL is deliberately
    NOT a member -- the two shapes carry it under different names, so callers
    pass an accessor and the shape mapping stays at the pipeline boundary
    that owns it.
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
    """What the domain scheduler reads off a worker's result.

    The breaker, latency-estimator and failure-bookkeeping paths below consult
    ``.status`` and ``.elapsed_ms``; nothing else about the result shape is
    scheduler business. Declaring the requirement as a protocol keeps ``run``
    generic over the crawl (``EngineCrawlResult``) and render (``CrawlResult``)
    pipelines without naming either, and without a union leaking into the
    caller's type.

    ``elapsed_ms`` is declared rather than ``getattr``-ed because the two
    shapes carry it in different places: ``CrawlResult`` has the field and no
    ``diagnostics`` at all, while ``EngineCrawlResult`` computes it from
    ``diagnostics`` or its first attempt. A member only one of them has is a
    member the scheduler silently skips for the other, which is how every
    render-pipeline latency went unlearned while the estimator looked
    correctly fed from crawl traffic alone.
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
# The worker is parameterized on the job it accepts. The alias is written with
# a bound TypeVar (not a contravariant one) because mypy cannot solve a
# contravariant TypeVar from a bare-alias parameter -- it left the call sites
# unsolved and silently widened to the protocol's own type.
SchedulerWorker = Callable[[_Job], Awaitable[_Result]]


class _TokenBucket:
    """Token bucket rate limiter: refills at ``rate`` tokens/sec, caps at ``capacity``."""

    def __init__(self, rate: float, capacity: int = 5) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def consume(self, tokens: float = 1.0) -> float:
        """Return seconds to wait before consuming ``tokens`` (0 if available)."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return 0.0
        deficit = tokens - self.tokens
        return deficit / self.rate


# Upper bound on how long any single job will sleep on a domain cooldown.
_MAX_COOLDOWN_WAIT = 300.0

# Purge the cooldown/circuit mappings at most once per this many
# set_domain_cooldown calls, so a crawl with many 429s does not rebuild both
# dicts on every rate-limited result. ``run`` still purges up front.
_COOLDOWN_PURGE_INTERVAL = 100

# What a result's STATUS means for the domain's health, over the whole
# ResultStatus space rather than the two values the branch used to name.
#
# The verdict each status carries is the one CLASSIFICATION_STATUS_BY_LABEL
# already documents, read as a statement about the DOMAIN rather than the page:
#  * success   -- usable content, or freshness proved by the caller's validators.
#                 The host answered, so the domain's counters reset.
#  * blocked   -- "the page exists, we were kept out" (anti-bot, consent). The
#                 fetch worked and the host is healthy, so this is a success
#                 for health purposes: a site that only ever walls us is not a
#                 site that needs backing off.
#  * failed    -- retrieved but unusable, or the transport died. A real fault
#                 against this domain.
#  * retry_exhausted -- the browser retry loop spent all its attempts. The
#                 table's own comment says the page may be perfectly renderable
#                 later, so the verdict describes the ATTEMPT, not the page --
#                 but repeatedly exhausting every attempt against one host is
#                 precisely the pressure a domain breaker exists to relieve, and
#                 leaving it unaccounted meant such a domain never tripped.
#  * skipped   -- robots disallowed: "we chose not to fetch". Never a failure;
#                 counting it would let a site's robots.txt degrade its own
#                 crawl rate, which is the opposite of what robots is for.
#  * disallowed -- a policy refusal (invalid URL, private target). Says nothing
#                 whatsoever about the domain, and is usually OUR decision.
# Exhaustive on purpose: see test_status_accounting_is_total.
_STATUS_ACCOUNTING: dict[str, str] = {
    "success": "success",
    "blocked": "success",
    "failed": "failure",
    "retry_exhausted": "failure",
    "skipped": "none",
    "disallowed": "none",
}


class DomainScheduler:
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
    ) -> None:
        self._global_limit = max(1, global_limit)
        self._per_domain_limit = max(1, per_domain_limit)
        self._delay_seconds = max(0.0, delay_seconds)
        # Per-domain minimum delay (e.g. a robots.txt Crawl-delay directive),
        # applied in addition to the global/adaptive delay so a host's stated
        # politeness floor is always respected.
        self._domain_min_delay: dict[str, float] = defaultdict(float)
        self._max_rps = max(0.0, max_rps)
        self._circuit_threshold = max(1, circuit_threshold)
        # Circuit breaker cooldown is now exponential: the first trip waits
        # ``base`` seconds, each subsequent consecutive trip doubles (capped at
        # ``circuit_cooldown``). A poisoned/tarpit domain is thus parked for
        # progressively longer without a manual reset.
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
        self._token_buckets: dict[str, _TokenBucket] = {}

        # Circuit breaker: consecutive failures per domain
        self._domain_failures: dict[str, int] = defaultdict(int)
        self._circuit_open_until: dict[str, float] = {}

        # Adaptive throttling state: EMA of observed latency (ms) per domain.
        self._domain_latency_ema: dict[str, float] = {}

        # Per-job per-domain strategy pinning: once a domain proves it needs a
        # particular strategy (e.g., browser), subsequent fetches for that
        # domain in the same job start at the pinned strategy instead of
        # re-climbing the ladder from the cheapest strategy.
        self._domain_strategy_pins: dict[str, str] = {}

        # Every rate-limited result calls set_domain_cooldown; purging both
        # dicts per call made a crawl with many 429s quadratic. The purge is
        # amortized to one per ``_COOLDOWN_PURGE_INTERVAL`` sets (``run`` still
        # purges up front), so the mappings stay bounded without per-429 work.
        self._sets_since_purge = 0

    def set_domain_cooldown(self, domain: str, seconds: float) -> None:
        """Set a cooldown period for a domain after a rate limit (429) response.

        Also purges domain cooldowns/circuits that have already elapsed, so the
        mappings cannot leak memory across a long-lived service — but on an
        amortized cadence, since every rate-limited result reaches here
        and rebuilding both dicts per call is quadratic in the number of 429s.
        """
        if seconds > 0:
            self._domain_cooldowns[domain] = time.time() + seconds
        self._sets_since_purge += 1
        if self._sets_since_purge >= _COOLDOWN_PURGE_INTERVAL:
            self._sets_since_purge = 0
            self._purge_expired()

    def _record_result_health(self, domain: str, result: SchedulerResult) -> None:
        """Apply a result's domain-health verdict. See _STATUS_ACCOUNTING.

        Its own method because it is one decision, not part of running a job:
        inlined it pushed the enclosing ``async with`` in run_one too long to
        read, and the verdict it reads is the whole
        question of what a status means for a domain's health.
        """
        accounting = _STATUS_ACCOUNTING.get(result.status, "none")
        if accounting == "failure":
            self.record_failure(domain)
            return
        if accounting != "success":
            return
        self.record_success(domain)
        # Feed observed latency into the adaptive estimator. One declared
        # member, satisfied by both result shapes, so this reads the same
        # whether the job came through the crawl or the render pipeline.
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
        # Each completed batch of ``threshold`` failures escalates one step:
        # trip 1 -> base, trip 2 -> base*2, trip 3 -> base*4,... capped. A
        # domain that fails 40 times in a row is parked for the max cooldown.
        n = self._domain_failures[domain] // self._circuit_threshold
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
            # Cooldown elapsed — half-open; reset counters and allow retry.
            # Resetting the trip count restarts the exponential backoff so a
            # recovered domain is not penalised by a stale escalation.
            del self._circuit_open_until[domain]
            self._domain_failures[domain] = 0
            self._circuit_trip_count[domain] = 0
            return False
        return True

    def record_latency(self, domain: str, latency_ms: float) -> None:
        """Feed observed response latency (ms) into the adaptive delay estimator.

        Negative is the only rejected value, and it is rejected because it is a
        bad measurement rather than a fast response: adopting one would pull the
        EMA below any delay actually observed and speed the domain up on the
        strength of a clock that went backwards.

        There is deliberately no ``None`` case. The one production caller reads
        ``SchedulerResult.elapsed_ms``, a non-optional ``int``, behind ``if
        latency:`` -- no measurement means nothing to record, and "no
        measurement" is decided at the call site where the result is in hand.
        """
        if latency_ms < 0:
            return
        prev = self._domain_latency_ema.get(domain)
        alpha = self._adaptive_smoothing
        self._domain_latency_ema[domain] = (
            latency_ms if prev is None else prev * (1.0 - alpha) + latency_ms * alpha
        )

    def get_pinned_strategy(self, domain: str) -> str | None:
        """Return the strategy pinned for ``domain`` within this job, or None."""
        return self._domain_strategy_pins.get(domain)

    def set_pinned_strategy(self, domain: str, strategy: str) -> None:
        """Record the strategy that succeeded for ``domain`` so future fetches
        in this job start at this strategy instead of re-climbing the ladder."""
        self._domain_strategy_pins[domain] = strategy

    def _recompute_delay(self, domain: str) -> float:
        """Target per-domain delay (seconds) from the latency EMA."""
        lat = self._domain_latency_ema.get(domain)
        if lat is None:
            return self._delay_seconds
        target = (lat / 1000.0) * self._adaptive_latency_factor
        return min(self._adaptive_max_delay, max(self._adaptive_min_delay, target))

    def current_delay(self, domain: str) -> float:
        """Delay (seconds) to apply before the next request to ``domain``.

        When adaptive throttling is enabled this is latency-driven; otherwise the
        fixed ``delay_seconds`` is used. A per-domain robots Crawl-delay floor
        (set via :meth:`set_domain_min_delay`) is always respected on top of the
        adaptive/global delay.
        """
        base = self._delay_seconds if not self._adaptive else self._recompute_delay(domain)
        return max(base, self._domain_min_delay.get(domain, 0.0))

    def set_domain_min_delay(self, domain: str, seconds: float) -> None:
        """Enforce a minimum inter-request delay for a domain (robots Crawl-delay).

        Non-finite values are ignored and finite values are clamped to the
        politeness ceiling so a hostile robots.txt cannot install an unbounded
        per-domain floor — the cap is enforced at the write, the
        chokepoint where a delay becomes a scheduler wait.
        """
        if seconds and seconds > 0 and math.isfinite(seconds):
            clamped = min(float(seconds), _MAX_COOLDOWN_WAIT)
            self._domain_min_delay[domain] = max(self._domain_min_delay.get(domain, 0.0), clamped)

    def _purge_expired(self) -> None:
        """Drop cooldown and circuit-breaker entries that have already elapsed."""
        now = time.time()
        self._domain_cooldowns = {d: exp for d, exp in self._domain_cooldowns.items() if exp > now}
        now_mono = time.monotonic()
        self._circuit_open_until = {d: exp for d, exp in self._circuit_open_until.items() if exp > now_mono}

    async def run(
        self,
        jobs: list[_Job],
        worker: SchedulerWorker[_Job, _Result],
        on_result: Callable[[int, _Result], Awaitable[None]] | None = None,
        *,
        failed_result: Callable[[_Job, BaseException], _Result],
    ) -> dict[int, _Result] | None:
        """Run ``jobs`` through ``worker``.

        When ``on_result`` is provided, each completed result is handed to it
        immediately (e.g. streamed to the on-disk result spill) and is NOT
        retained in memory -- peak RAM then tracks the in-flight set rather than
        the whole batch. In that mode ``run`` returns
        ``None``; otherwise it returns a mapping of ``job.index -> result``.

        The mapping is keyed by job index rather than positionally so callers
        can align each result with its own job regardless of completion order,
        and so the result store does not have to allocate a slot for every
        index in the batch (a high-index batch would otherwise allocate a list
        of ``max(index)`` entries). Jobs whose worker raised are absent from the
        mapping; the caller decides how to represent them.
        """
        # Purge stale state up front so dicts cannot grow without bound.
        self._purge_expired()
        global_gate = asyncio.Semaphore(self._global_limit)
        domain_gates: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_domain_limit),
        )
        # Retain results in memory only when the caller needs them back
        # (recursive frontier expansion). With on_result set, results are spilled
        # per-job and never accumulated here, bounding batch RAM.
        results: dict[int, _Result] = {}

        # ``on_result`` and the returned mapping are mutually exclusive delivery
        # channels (spill-to-disk vs. in-memory). The single sink below keeps
        # that invariant in one place: narrowing ``dict | None`` inside the
        # closure could not track which branch held, and the two delivery
        # channels had historically drifted: a slot silently vanished.
        #
        # Both a worker's return and the failure envelopes are ``_Result``: the
        # caller supplies ``failed_result`` so the shape a broken job gets is
        # decided by the pipeline that owns the contract, not assumed here.
        #
        # A sink failure is LOGGED and swallowed. It must not propagate: the
        # caller's sink raising is not evidence the job failed, so letting it out
        # was answered with a failure envelope for a page that succeeded -- then
        # delivered through the same broken sink, and discarded by gather's
        # return_exceptions with no log line. The "job vanished" shape.
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
            # Domain cooldown MUST be awaited *before* acquiring the global
            # semaphore, otherwise a single rate-limited domain would block
            # every other domain's concurrency slot (cooldown deadlock).
            cooldown_expiry = self._domain_cooldowns.get(job.domain)
            if cooldown_expiry is not None:
                now = time.time()
                if now < cooldown_expiry:
                    # Cap the wait: a hostile Retry-After must not park this
                    # coroutine (and the whole gather) for years. Beyond the cap
                    # the job proceeds and the origin will re-assert its limit.
                    await asyncio.sleep(min(cooldown_expiry - now, _MAX_COOLDOWN_WAIT))

            # Circuit breaker: if open, skip the network request entirely.
            if self.is_circuit_open(job.domain):
                # In on_result mode ``results`` stays empty, so the failed
                # envelope must go through the same channel as normal results.
                # Writing results[job.index] directly here raised TypeError,
                # which gather(return_exceptions=True) swallowed -- the job
                # then got no envelope, was not spilled, and silently vanished
                # from /v1/crawl.
                #
                # The classification and redaction this branch used to inline
                # now live in the pipeline's ``failed_result``: the crawl
                # pipeline keeps its network_error/circuit_breaker_open
                # envelope, and the render pipeline builds its own shape.
                await deliver(
                    job.index,
                    failed_result(job, _CircuitBreakerOpen(self._domain_cooldowns.get(job.domain))),
                )
                return

            # Token bucket rate limiting.
            if self._max_rps > 0:
                bucket = self._token_buckets.get(job.domain)
                if bucket is None:
                    bucket = _TokenBucket(rate=self._max_rps)
                    self._token_buckets[job.domain] = bucket
                wait_seconds = bucket.consume(1.0)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

            async with domain_gates[job.domain]:
                # Apply jitter to the (possibly adaptive) per-domain delay while
                # holding ONLY the per-domain gate. Sleeping inside global_gate
                # would burn a global concurrency slot on pure waiting and starve
                # every other domain (throughput fix).
                base_delay = self.current_delay(job.domain)
                delay_sleep = jittered_delay(base_delay)
                await asyncio.sleep(delay_sleep)
                async with global_gate:
                    try:
                        result = await worker(job)
                        self._record_result_health(job.domain, result)
                    except Exception as error:
                        self.record_failure(job.domain)
                        # Chokepoint for "a worker raised": build a classified
                        # terminal result HERE so no caller can ever observe an
                        # absent job. Previously every caller improvised its own
                        # envelope (recursive.py hand-built one; the sync
                        # ``on_result`` path silently dropped the page), and the
                        # two drifted in shape and classification. The envelope
                        # itself comes from ``failed_result`` so a pipeline
                        # always gets back its own result shape.
                        await deliver(job.index, failed_result(job, error))
                        return
                    # Delivery sits OUTSIDE the worker's ``try`` (which covers
                    # the work, and only the work): a raising sink is a delivery
                    # failure, not a reason to charge the domain's breaker.
                    await deliver(job.index, result)

        # Return_exceptions=True: a single job failure must not cancel the
        # other in-flight jobs. Exceptions are captured here; the
        # failing job is simply absent from the returned mapping.
        await asyncio.gather(*(run_one(job) for job in jobs), return_exceptions=True)
        if on_result is not None:
            return None
        return results

    @property
    def domain_cooldowns(self) -> dict[str, float]:
        return dict(self._domain_cooldowns)
