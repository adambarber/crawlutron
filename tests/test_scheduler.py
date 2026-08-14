"""DomainScheduler real behavior: jitter, domain cooldowns, job scheduling (no mocks)."""

import asyncio
import time
import unittest

from app.models import CrawlJob, CrawlResult
from app.scheduler import DomainScheduler


def make_job(index: int, url: str) -> CrawlJob:
    return CrawlJob(index=index, url=url, domain=url.split("/")[2], options=None)


async def ok_worker(job: CrawlJob) -> CrawlResult:
    return CrawlResult(
        url=job.url,
        status="success",
        classification="rendered",
        final_url=job.url,
        html="<html></html>",
        text="ok",
        attempts=1,
        elapsed_ms=0,
        domain=job.domain,
        metadata={},
    )


class SchedulerTest(unittest.TestCase):
    def test_jitter_in_range(self) -> None:
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=3, delay_seconds=1.0)
        # Jitter formula is delay * uniform(0.8, 1.2); sample many draws.
        delays = [scheduler._delay_seconds * __import__("random").uniform(0.8, 1.2) for _ in range(50)]
        self.assertTrue(all(0.7 <= d <= 1.3 for d in delays))

    def test_set_domain_cooldown_is_in_future(self) -> None:
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=3, delay_seconds=0.1)
        scheduler.set_domain_cooldown("example.com", 5.0)
        self.assertIn("example.com", scheduler._domain_cooldowns)
        expiry = scheduler._domain_cooldowns["example.com"]
        self.assertGreater(expiry, time.time())
        self.assertGreater(expiry - time.time(), 4.0)

    def test_cooldown_delay_applied_to_domain(self) -> None:
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.05)
        scheduler.set_domain_cooldown("example.com", 0.3)
        job = make_job(0, "http://example.com/")
        started = time.monotonic()
        results = asyncio.run(scheduler.run([job], ok_worker))
        elapsed = time.monotonic() - started
        self.assertEqual(results[0].status, "success")
        # The cooldown should have forced a sleep of roughly the cooldown duration.
        self.assertGreater(elapsed, 0.25)

    def test_run_returns_results_in_order(self) -> None:
        scheduler = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=0.0)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]
        results = asyncio.run(scheduler.run(jobs, ok_worker))
        self.assertEqual([r.url for r in results], [f"http://example.com/{i}" for i in range(3)])

    def test_delay_applied_between_same_domain_jobs(self) -> None:
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.1)
        jobs = [make_job(0, "http://example.com/a"), make_job(1, "http://example.com/b")]
        started = time.monotonic()
        asyncio.run(scheduler.run(jobs, ok_worker))
        elapsed = time.monotonic() - started
        # Two same-domain jobs with ~0.1s jittered delay should take at least ~0.05s.
        self.assertGreaterEqual(elapsed, 0.05)


if __name__ == "__main__":
    unittest.main()
