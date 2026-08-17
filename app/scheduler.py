from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from .models import CrawlJob, CrawlResult, EngineCrawlResult, CrawlStrategyAttempt

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


class DomainScheduler:
    def __init__(
        self,
        *,
        global_limit: int,
        per_domain_limit: int,
        delay_seconds: float,
        max_rps: float = 0.0,
        circuit_threshold: int = 5,
        circuit_cooldown: float = 60.0,
    ) -> None:
        self._global_limit = max(1, global_limit)
        self._per_domain_limit = max(1, per_domain_limit)
        self._delay_seconds = max(0.0, delay_seconds)
        self._max_rps = max(0.0, max_rps)
        self._circuit_threshold = max(1, circuit_threshold)
        self._circuit_cooldown = max(1.0, circuit_cooldown)

        # Track when domains are allowed to be hit again to implement domain-level cooldowns
        self._domain_cooldowns: dict[str, float] = {}

        # Token buckets per domain for RPS rate limiting
        self._token_buckets: dict[str, _TokenBucket] = {}

        # Circuit breaker: consecutive failures per domain
        self._domain_failures: dict[str, int] = defaultdict(int)
        self._circuit_open_until: dict[str, float] = {}

    def set_domain_cooldown(self, domain: str, seconds: float) -> None:
        """Set a cooldown period for a domain after a rate limit (429) response.

        Also purges any other domain cooldowns that have already elapsed so the
        mapping cannot leak memory across a long-lived service.
        """
        if seconds > 0:
            self._domain_cooldowns[domain] = time.time() + seconds
        self._purge_expired()

    def record_success(self, domain: str) -> None:
        """Reset the failure counter for a domain after a successful crawl."""
        self._domain_failures[domain] = 0

    def record_failure(self, domain: str) -> None:
        """Increment the failure counter; trip the circuit breaker if threshold reached."""
        self._domain_failures[domain] += 1
        if self._domain_failures[domain] >= self._circuit_threshold:
            self._circuit_open_until[domain] = time.monotonic() + self._circuit_cooldown

    def is_circuit_open(self, domain: str) -> bool:
        """Check if the circuit breaker is open for a domain."""
        expiry = self._circuit_open_until.get(domain)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            # Cooldown elapsed — half-open; reset counters and allow retry.
            del self._circuit_open_until[domain]
            self._domain_failures[domain] = 0
            return False
        return True

    def _purge_expired(self) -> None:
        """Drop cooldown and circuit-breaker entries that have already elapsed."""
        now = time.time()
        self._domain_cooldowns = {d: exp for d, exp in self._domain_cooldowns.items() if exp > now}
        now_mono = time.monotonic()
        self._circuit_open_until = {d: exp for d, exp in self._circuit_open_until.items() if exp > now_mono}

    async def run(self, jobs: list[CrawlJob], worker: SchedulerWorker) -> list[EngineCrawlResult]:
        # Purge stale state up front so dicts cannot grow without bound.
        self._purge_expired()
        global_gate = asyncio.Semaphore(self._global_limit)
        domain_gates: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._per_domain_limit),
        )
        result_count = max((job.index for job in jobs), default=-1) + 1
        results: list[EngineCrawlResult | None] = [None] * result_count

        async def run_one(job: CrawlJob) -> None:
            # Domain cooldown MUST be awaited *before* acquiring the global
            # semaphore, otherwise a single rate-limited domain would block
            # every other domain's concurrency slot (cooldown deadlock).
            cooldown_expiry = self._domain_cooldowns.get(job.domain)
            if cooldown_expiry is not None:
                now = time.time()
                if now < cooldown_expiry:
                    await asyncio.sleep(cooldown_expiry - now)

            # Circuit breaker: if open, skip the network request entirely.
            if self.is_circuit_open(job.domain):
                results[job.index] = EngineCrawlResult(
                    original_url=job.url,
                    normalized_url=job.normalized_url,
                    final_url=None,
                    status="failed",
                    classification="network_error",
                    selected_strategy=None,
                    attempted_strategies=[
                        CrawlStrategyAttempt(
                            strategy="",  # type: ignore[arg-type]
                            status="failed",
                            classification="network_error",
                            http_status=None,
                            content_type=None,
                            elapsed_ms=0,
                            error="circuit_breaker_open",
                        )
                    ],
                    http_status=None,
                    headers={},
                    content_type=None,
                    html=None,
                    text=None,
                    markdown=None,
                    extraction_method=None,
                    quality_score=0.0,
                    classification_evidence=[],
                    diagnostics={"circuit_breaker": True, "error": "circuit_breaker_open"},
                    discovered_link_count=0,
                    discovered_links_truncated=False,
                    discovered_links=[],
                    error="circuit_breaker_open",
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

            async with global_gate:
                async with domain_gates[job.domain]:
                    # Apply jitter to the per-domain delay
                    delay_sleep = self._delay_seconds * random.uniform(0.8, 1.2) if self._delay_seconds > 0 else 0
                    await asyncio.sleep(delay_sleep)
                    try:
                        result = await worker(job)
                        if result.status == "success":
                            self.record_success(job.domain)
                        elif result.status in ("failed", "network_error"):
                            self.record_failure(job.domain)
                        results[job.index] = result
                    except Exception:
                        self.record_failure(job.domain)
                        raise

        await asyncio.gather(*(run_one(job) for job in jobs))
        return [result for result in results if result is not None]  # type: ignore[return-value]

    @property
    def domain_cooldowns(self) -> dict[str, float]:
        return dict(self._domain_cooldowns)
