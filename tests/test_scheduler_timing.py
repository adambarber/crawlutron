"""Scheduler timing behavior tests.

``fast_sleeps`` is autouse by default, so timing tests that need real wall-clock
sleeps must explicitly request the ``real_sleeps`` fixture to verify the actual
delay behavior.
"""
import time

from _testkit import make_job, make_result

from app.models import CrawlJob
from app.scheduler import DomainScheduler


async def _ok(job: CrawlJob):
    return make_result(job.url, job.domain)


async def _run_two_same_domain(delay: float = 0.1) -> float:
    """Drive two same-domain jobs through the scheduler; return elapsed seconds."""
    scheduler = DomainScheduler(
        global_limit=1, per_domain_limit=1, delay_seconds=delay, adaptive=False,
    )
    jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
    started = time.monotonic()
    await scheduler.run(jobs, _ok)
    return time.monotonic() - started


async def test_real_sleeps_exercise_per_domain_delay(real_sleeps):
    # Explicitly request real sleeps to verify the per-domain delay is awaited.
    elapsed = await _run_two_same_domain(delay=0.1)
    # Two same-domain jobs each await the per-domain delay (~0.1s w/ jitter).
    assert elapsed >= 0.05, f"per-domain delay not exercised (got {elapsed:.3f}s)"


async def test_real_sleeps_exercise_domain_cooldown(real_sleeps):
    scheduler = DomainScheduler(
        global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive=False,
    )
    scheduler.set_domain_cooldown("example.com", 0.3)
    job = make_job(0, "https://example.com/")
    started = time.monotonic()
    results = await scheduler.run([job], _ok)
    elapsed = time.monotonic() - started
    assert results[0].status == "success"
    # The single job must await the ~0.3s cooldown before running.
    assert elapsed > 0.25, f"cooldown not exercised (got {elapsed:.3f}s)"


async def test_default_fast_sleeps_skip_delay():
    # fast_sleeps is autouse by default, so no delay should be awaited.
    elapsed = await _run_two_same_domain(delay=0.1)
    assert elapsed < 0.1, f"fast_sleeps should skip real delay (got {elapsed:.3f}s)"
