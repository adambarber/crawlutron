from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from .models import CrawlJob, CrawlResult, EngineCrawlResult, CrawlStrategyAttempt
from .crawl.result_helpers import _failed_engine_result

SchedulerWorker = Callable[[CrawlJob], Awaitable[EngineCrawlResult]]


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
        # politeness floor is always respected (adversarial perf #2.6).
        self._domain_min_delay: dict[str, float] = defaultdict(float)
        self._max_rps = max(0.0, max_rps)
        self._circuit_threshold = max(1, circuit_threshold)
        # Circuit breaker cooldown is now exponential: the first trip waits
        # ``base`` seconds, each subsequent consecutive trip doubles (capped at
        # ``circuit_cooldown``). A poisoned/tarpit domain is thus parked for
        # progressively longer without a manual reset (adversarial perf #2.1).
        self._circuit_cooldown_base = max(1.0, circuit_cooldown_base)
        self._circuit_cooldown_max = max(self._circuit_cooldown_base, circuit_cooldown)
        # Adaptive throttling (AutoThrottle-style, #6): per-domain delay is driven by
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

    def set_domain_cooldown(self, domain: str, seconds: float) -> None:
        """Set a cooldown period for a domain after a rate limit (429) response.

        Also purges any other domain cooldowns that have already elapsed so the
        mapping cannot leak memory across a long-lived service.
        """
        if seconds > 0:
            self._domain_cooldowns[domain] = time.time() + seconds
        self._purge_expired()

    def record_success(self, domain: str) -> None:
        """Reset the failure counter (and exponential backoff) for a domain."""
        self._domain_failures[domain] = 0
        self._circuit_trip_count[domain] = 0

    def record_failure(self, domain: str) -> None:
        """Increment the failure counter; escalate the circuit-breaker cooldown
        exponentially with each threshold-crossing batch of failures."""
        self._domain_failures[domain] += 1
        # Each completed batch of ``threshold`` failures escalates one step:
        # trip 1 -> base, trip 2 -> base*2, trip 3 -> base*4, ... capped. A
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
        """Feed observed response latency (ms) into the adaptive delay estimator (#6)."""
        if latency_ms is None or latency_ms < 0:
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
        """Target per-domain delay (seconds) from the latency EMA (#6)."""
        lat = self._domain_latency_ema.get(domain)
        if lat is None:
            return self._delay_seconds
        target = (lat / 1000.0) * self._adaptive_latency_factor
        return min(self._adaptive_max_delay, max(self._adaptive_min_delay, target))

    def current_delay(self, domain: str) -> float:
        """Delay (seconds) to apply before the next request to ``domain`` (#6).

        When adaptive throttling is enabled this is latency-driven; otherwise the
        fixed ``delay_seconds`` is used. A per-domain robots Crawl-delay floor
        (set via :meth:`set_domain_min_delay`) is always respected on top of the
        adaptive/global delay (adversarial perf #2.6).
        """
        base = self._delay_seconds if not self._adaptive else self._recompute_delay(domain)
        return max(base, self._domain_min_delay.get(domain, 0.0))

    def set_domain_min_delay(self, domain: str, seconds: float) -> None:
        """Enforce a minimum inter-request delay for a domain (robots Crawl-delay)."""
        if seconds and seconds > 0:
            self._domain_min_delay[domain] = max(
                self._domain_min_delay.get(domain, 0.0), float(seconds)
            )

    def _purge_expired(self) -> None:
        """Drop cooldown and circuit-breaker entries that have already elapsed."""
        now = time.time()
        self._domain_cooldowns = {d: exp for d, exp in self._domain_cooldowns.items() if exp > now}
        now_mono = time.monotonic()
        self._circuit_open_until = {d: exp for d, exp in self._circuit_open_until.items() if exp > now_mono}

    async def run(
        self,
        jobs: list[CrawlJob],
        worker: SchedulerWorker,
        on_result: "Callable[[int, EngineCrawlResult], Awaitable[None]] | None" = None,
    ) -> "list[EngineCrawlResult] | None":
        """Run ``jobs`` through ``worker``.

        When ``on_result`` is provided, each completed result is handed to it
        immediately (e.g. streamed to the on-disk result spill) and is NOT
        retained in memory -- peak RAM then tracks the in-flight set rather than
        the whole batch (adversarial perf #3.1). In that mode ``run`` returns
        ``None``; otherwise it returns the ordered list of results (used by the
        recursive frontier expansion).
        """
        # Purge stale state up front so dicts cannot grow without bound.
        self._purge_expired()
        global_gate = asyncio.Semaphore(self._global_limit)
        domain_gates: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_domain_limit),
        )
        result_count = max((job.index for job in jobs), default=-1) + 1
        # Retain results in memory only when the caller needs them back
        # (recursive frontier expansion). With on_result set, results are spilled
        # per-job and never accumulated here, bounding batch RAM (#3.1).
        results: "list[EngineCrawlResult | None] | None" = None if on_result else [None] * result_count

        async def run_one(job: CrawlJob) -> None:
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
                results[job.index] = _failed_engine_result(
                    original_url=job.url,
                    normalized_url=job.normalized_url,
                    strategy="",
                    classification="network_error",
                    error="circuit_breaker_open",
                    diagnostics={"circuit_breaker": True, "error": "circuit_breaker_open"},
                    next_page_url=None,
                )
                return

            # Token bucket rate limiting (#1: Advanced Client-Side Rate Limiting)
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
                delay_sleep = base_delay * random.uniform(0.8, 1.2) if base_delay > 0 else 0
                await asyncio.sleep(delay_sleep)
                async with global_gate:
                    try:
                        result = await worker(job)
                        if result.status == "success":
                            self.record_success(job.domain)
                            # Feed observed latency into the adaptive estimator (#6).
                            # Render results (CrawlResult) may not carry diagnostics, so
                            # guard attribute access.
                            diag = getattr(result, "diagnostics", None)
                            latency = None
                            if isinstance(diag, dict):
                                rt = diag.get("runtime") or {}
                                latency = rt.get("elapsed_ms")
                            if latency:
                                self.record_latency(job.domain, latency)
                        elif result.status in ("failed", "network_error"):
                            self.record_failure(job.domain)
                        if on_result is not None:
                            # Stream to the caller immediately; do not retain.
                            await on_result(job.index, result)
                        else:
                            results[job.index] = result
                    except Exception:
                        self.record_failure(job.domain)
                        raise

        # return_exceptions=True: a single job failure must not cancel the
        # other in-flight jobs (HIGH #6). Exceptions are captured here and the
        # failing job is simply dropped from the results list.
        await asyncio.gather(*(run_one(job) for job in jobs), return_exceptions=True)
        if on_result is not None:
            return None
        return [result for result in results if result is not None]  # type: ignore[return-value]

    @property
    def domain_cooldowns(self) -> dict[str, float]:
        return dict(self._domain_cooldowns)
