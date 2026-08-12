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
        """Set a cooldown period for a domain after a rate limit (429) response."""
        if seconds > 0:
            self._domain_cooldowns[domain] = time.time() + seconds

    async def run(self, jobs: list[CrawlJob], worker: CrawlWorker) -> list[CrawlResult]:
        global_gate = asyncio.Semaphore(self._global_limit)
        domain_gates: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(self._per_domain_limit))
        result_count = max((job.index for job in jobs), default=-1) + 1
        results: list[CrawlResult | None] = [None] * result_count

        async def run_one(job: CrawlJob) -> None:
            async with global_gate:
                async with domain_gates[job.domain]:
                    # Check if we're in a cooldown period for this domain
                    if self._delay_seconds and self._domain_cooldowns:
                        now = time.time()
                        for domain, expiry in self._domain_cooldowns.items():
                            if domain == job.domain and now < expiry:
                                remaining = expiry - now
                                await asyncio.sleep(remaining)
                    # Apply jitter to the delay
                    delay_sleep = self._delay_seconds * random.uniform(0.8, 1.2) if self._delay_seconds > 0 else 0
                    await asyncio.sleep(delay_sleep)
                    results[job.index] = await worker(job)

        await asyncio.gather(*(run_one(job) for job in jobs))
        return [result for result in results if result is not None]
