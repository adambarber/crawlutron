"""TDD tests for HIGH #6: scheduler must not cancel all jobs on one failure.

``asyncio.gather`` without ``return_exceptions=True`` cancels the remaining
in-flight jobs when a single job raises, aborting the whole batch on one
transient error.
"""
import pytest

from app.models import CrawlJob, CrawlStrategyAttempt, EngineCrawlResult
from app.scheduler import DomainScheduler


def make_job(index: int, url: str) -> CrawlJob:
    return CrawlJob(index=index, url=url, domain=url.split("/")[2], options=None)


def _make_result(url: str, domain: str) -> EngineCrawlResult:
    return EngineCrawlResult(
        original_url=url, normalized_url=url, final_url=url,
        status="success", classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[CrawlStrategyAttempt(
            strategy="HTTP_DECLARED_CRAWLER", status="success",
            classification="rendered", http_status=200,
            content_type="text/html", elapsed_ms=0, error=None)],
        http_status=200, headers={}, content_type="text/html",
        html="<html></html>", text="ok", markdown="ok",
        extraction_method="http_html", quality_score=1.0,
        classification_evidence=[], diagnostics={},
        discovered_link_count=0, discovered_links_truncated=False,
        discovered_links=[], error=None, next_page_url=None,
    )


class TestSchedulerResilience:
    async def test_scheduler_continues_other_jobs_on_single_failure(self):
        """One failing job must not cancel the others; results for the
        successful jobs are still returned."""
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=5, delay_seconds=0.0)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]

        async def failing_worker(job: CrawlJob) -> EngineCrawlResult:
            if job.index == 1:
                raise RuntimeError("transient failure")
            return _make_result(job.url, job.domain)

        results = await scheduler.run(jobs, failing_worker)
        assert len(results) == 2, results
        assert all(r.status == "success" for r in results)
        assert {r.normalized_url for r in results} == {
            "http://example.com/0",
            "http://example.com/2",
        }
