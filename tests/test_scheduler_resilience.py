"""TDD tests for HIGH #6: scheduler must not cancel all jobs on one failure.

``asyncio.gather`` without ``return_exceptions=True`` cancels the remaining
in-flight jobs when a single job raises, aborting the whole batch on one
transient error.
"""

from _testkit import make_job, make_result

from app.models import CrawlJob, EngineCrawlResult
from app.scheduler import DomainScheduler


class TestSchedulerResilience:
    async def test_scheduler_continues_other_jobs_on_single_failure(self):
        """One failing job must not cancel the others; results for the
        successful jobs are still returned."""
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=5, delay_seconds=0.0)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]

        async def failing_worker(job: CrawlJob) -> EngineCrawlResult:
            if job.index == 1:
                raise RuntimeError("transient failure")
            return make_result(job.url, job.domain)

        results = await scheduler.run(jobs, failing_worker)
        assert len(results) == 2, results
        assert all(r.status == "success" for r in results.values())
        assert {r.normalized_url for r in results.values()} == {
            "http://example.com/0",
            "http://example.com/2",
        }
