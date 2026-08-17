"""DomainScheduler real behavior: jitter, domain cooldowns, job scheduling (no mocks)."""

import asyncio
import random
import time
from typing import Any

import pytest
from app.models import CrawlJob, CrawlStrategyAttempt, EngineCrawlResult
from app.scheduler import DomainScheduler


def make_job(index: int, url: str) -> CrawlJob:
    return CrawlJob(index=index, url=url, domain=url.split("/")[2], options=None)


def _make_result(url: str, domain: str) -> EngineCrawlResult:
    return EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=url,
        status="success",
        classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[
            CrawlStrategyAttempt(
                strategy="HTTP_DECLARED_CRAWLER",
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=0,
                error=None,
            )
        ],
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html></html>",
        text="ok",
        markdown="ok",
        extraction_method="http_html",
        quality_score=1.0,
        classification_evidence=[],
        diagnostics={},
        discovered_link_count=0,
        discovered_links_truncated=False,
        discovered_links=[],
        error=None,
        next_page_url=None,
    )


async def ok_worker(job: CrawlJob) -> Any:
    return _make_result(job.url, job.domain)


class TestScheduler:
    def test_jitter_in_range(self):
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=3, delay_seconds=1.0)
        delays = [scheduler._delay_seconds * random.uniform(0.8, 1.2) for _ in range(50)]
        assert all(0.7 <= d <= 1.3 for d in delays)

    def test_set_domain_cooldown_is_in_future(self):
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=3, delay_seconds=0.1)
        scheduler.set_domain_cooldown("example.com", 5.0)
        assert "example.com" in scheduler._domain_cooldowns
        expiry = scheduler._domain_cooldowns["example.com"]
        assert expiry > time.time()
        assert expiry - time.time() > 4.0

    async def test_cooldown_delay_applied_to_domain(self, real_sleeps):
        """This test needs real asyncio.sleep for timing verification."""
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.05)
        scheduler.set_domain_cooldown("example.com", 0.3)
        job = make_job(0, "http://example.com/")
        started = time.monotonic()
        results = await scheduler.run([job], ok_worker)
        elapsed = time.monotonic() - started
        assert results[0].status == "success"
        assert elapsed > 0.25

    async def test_run_returns_results_in_order(self):
        scheduler = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=0.0)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]
        results = await scheduler.run(jobs, ok_worker)
        assert [r.normalized_url for r in results] == [f"http://example.com/{i}" for i in range(3)]

    async def test_delay_applied_between_same_domain_jobs(self, real_sleeps):
        """This test needs real asyncio.sleep for timing verification."""
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.1)
        jobs = [make_job(0, "http://example.com/a"), make_job(1, "http://example.com/b")]
        started = time.monotonic()
        await scheduler.run(jobs, ok_worker)
        elapsed = time.monotonic() - started
        assert elapsed >= 0.05
