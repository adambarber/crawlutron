"""Throughput regression test: per-domain delays must not hold global slots.

The politeness delay used to be slept *inside* ``global_gate``, so one slow
domain starved every other domain of concurrency for the whole delay.
"""

import asyncio
import time

from app.scheduler import DomainScheduler


class _Job:
    def __init__(self, index: int, domain: str):
        self.index = index
        self.domain = domain


class _PerDomainDelayScheduler(DomainScheduler):
    """Fixed delays that differ per domain (adaptive throttling off)."""

    def current_delay(self, domain: str) -> float:
        return 0.4 if domain == "slow.example.com" else 0.0


async def test_domain_delay_does_not_block_other_domains(real_sleeps):
    scheduler = _PerDomainDelayScheduler(
        global_limit=2,
        per_domain_limit=1,
        delay_seconds=0.0,
        adaptive=False,
    )
    starts: dict[int, float] = {}

    async def worker(job):
        starts[job.index] = time.monotonic()
        await asyncio.sleep(0.02)
        from app.models import EngineCrawlResult  # noqa: F401  (any value works)
        return "ok"

    t0 = time.monotonic()
    jobs = [_Job(0, "slow.example.com"), _Job(1, "fast.example.com")]
    await scheduler.run(jobs, worker)

    # fast.example.com must start immediately; with the old gate ordering it
    # waited >= 0.4s for the global slot burned by slow.example.com's delay.
    fast_start = starts[1] - t0
    slow_start = starts[0] - t0
    assert fast_start < 0.2, f"starved behind slow domain delay: {starts}"
    assert slow_start >= 0.3, f"slow domain delay not applied: {starts}"
