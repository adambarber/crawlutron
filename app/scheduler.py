from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from .models import CrawlJob, CrawlResult

CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult]]

class DomainScheduler:
    def __init__(self, *, global_limit: int, per_domain_limit: int, delay_seconds: float) -> None:
        self._global_limit = max(1, global_limit)
        self._per_domain_limit = max(1, per_domain_limit)
        self._delay_seconds = max(0.0, delay_seconds)
        # Track when domains are allowed to be hit again to implement domain-level cooldowns
        self._domain_cooldowns: dict[str, float] = {}

    def set_domain_cooldown(self, domain: str, seconds: float) -> None:
        """Set a cooldown period for a domain after a rate limit (429) response.

        Also purges any other domain cooldowns that have already elapsed so the
        mapping cannot leak memory across a long-lived service.
        """
        if seconds > 0:
            self._domain_cooldowns[domain] = time.time() + seconds
        self._purge_expired_cooldowns()

    def _purge_expired_cooldowns(self) -> None:
        """Drop cooldown entries that have already elapsed (memory-leak guard)."""
        now = time.time()
        self._domain_cooldowns = {domain: expiry for domain, expiry in self._domain_cooldowns.items() if expiry > now}

    async def run(self, jobs: list[CrawlJob], worker: CrawlWorker) -> list[CrawlResult]:
        # Purge stale cooldowns up front so the dict cannot grow without bound.
        self._purge_expired_cooldowns()
        global_gate = asyncio.Semaphore(self._global_limit)
        domain_gates: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(self._per_domain_limit))
        result_count = max((job.index for job in jobs), default=-1) + 1
        results: list[CrawlResult | None] = [None] * result_count

        async def run_one(job: CrawlJob) -> None:
            # Domain cooldown MUST be awaited *before* acquiring the global
            # semaphore, otherwise a single rate-limited domain would block
            # every other domain's concurrency slot (cooldown deadlock).
            cooldown_expiry = self._domain_cooldowns.get(job.domain)
            if cooldown_expiry is not None:
                now = time.time()
                if now < cooldown_expiry:
                    await asyncio.sleep(cooldown_expiry - now)
            async with global_gate:
                async with domain_gates[job.domain]:
                    # Apply jitter to the per-domain delay
                    delay_sleep = self._delay_seconds * random.uniform(0.8, 1.2) if self._delay_seconds > 0 else 0
                    await asyncio.sleep(delay_sleep)
                    results[job.index] = await worker(job)

        await asyncio.gather(*(run_one(job) for job in jobs))
        return [result for result in results if result is not None]
