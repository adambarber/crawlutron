"""Durable job batches derive their wall bound from the admitted policy snapshot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.service import job_runner
from crawlutron.service.effects import EffectContainer, EffectOutcome, run_crawl_with_claim
from crawlutron.storage.job_store import JobStatus, JobStore
from crawlutron.vocabulary.limits import MAX_TOTAL_URL_BUDGET_SECONDS
from tests._job_drivers import _seed_job, job_limits
from tests._job_harnesses import _execution_urls, drive_job, run_to_terminal
from tests._result_builders import engine_result

_URLS = tuple(f"https://example.test/page/{index}" for index in range(8))


class _ClaimStore:
    async def arenew_job_claim(self, job_id: str, token: str, *, now: float) -> bool:
        _ = job_id, token, now
        return True


@pytest.mark.parametrize(
    ("url_count", "url_budget"),
    [(1, 0.1), (2, 15.0), (8, MAX_TOTAL_URL_BUDGET_SECONDS)],
)
def test_batch_bound_scales_the_policy_url_budget(url_count: int, url_budget: float) -> None:
    payload = CrawlRequest(
        urls=list(_URLS[:url_count]),
        policy=CrawlPolicy(limits={"total_url_budget_seconds": url_budget}),
    )

    assert job_runner.job_batch_timeout_seconds(payload) == url_budget * url_count


def test_batch_bound_handles_a_single_url_payload() -> None:
    payload = CrawlRequest(
        url=_URLS[0],
        policy=CrawlPolicy(limits={"total_url_budget_seconds": 2.5}),
    )

    assert job_runner.job_batch_timeout_seconds(payload) == 2.5


@pytest.mark.real_sleeps
async def test_one_slow_url_does_not_fail_the_healthy_urls(job_store) -> None:
    slow_url = _URLS[0]
    attempts = 0

    async def _execute(payload, *, settings, shutdown_event):
        nonlocal attempts
        attempts += 1
        urls = await _execution_urls(payload, None)
        if len(urls) > 1:
            # The batch is slow; the one-URL retry passes answer at once.
            await asyncio.sleep(1.05)
        results = [
            engine_result(
                url,
                "url_budget_exceeded" if url.raw == slow_url else "rendered",
                status="failed" if url.raw == slow_url else "success",
            )
            for url in urls
        ]
        return {
            "request_id": "test",
            "results": results,
            "submitted_count": len(urls),
            "policy_schema_version": "test",
        }

    job_runner.install_execute_crawl(_execute)
    job = _seed_job(
        job_store,
        _URLS[0],
        job_limits(max_depth=1, total_url_budget_seconds=2.0),
        frontier=list(_URLS),
    )
    await drive_job(
        job.job_id,
        job_store,
        settings=CrawlerSettings(default_timeout_seconds=1),
    )
    finished = await run_to_terminal(job_store, job.job_id)

    assert finished.status == JobStatus.COMPLETED
    by_url = {result["normalized_url"]: result for result in finished.results}
    assert sum(result["status"] == "success" for result in finished.results) == 7
    assert by_url[slow_url]["status"] == "failed"
    assert attempts == 3


async def test_timed_out_batch_marks_only_the_unresolved_url(job_store, monkeypatch) -> None:
    fast_url = _URLS[1]
    slow_url = _URLS[0]
    calls = 0
    observed_bounds = []

    async def _timed_out(container, op, url, timeout, job_store, job_id, token):
        nonlocal calls
        calls += 1
        observed_bounds.append(timeout)
        return SimpleNamespace(
            outcome=EffectOutcome.TIMED_OUT,
            items=(engine_result(fast_url, "rendered"),),
            still_owned=True,
        )

    monkeypatch.setattr(job_runner, "run_crawl_with_claim", _timed_out)
    job = _seed_job(
        job_store,
        slow_url,
        job_limits(max_depth=1, total_url_budget_seconds=2.0),
        frontier=[slow_url, fast_url],
    )
    await drive_job(job.job_id, job_store)
    finished = await run_to_terminal(job_store, job.job_id)

    assert finished.status == JobStatus.COMPLETED
    assert calls == 1
    # The claim's wall bound is the batch's policy budget, not the settings default.
    assert observed_bounds == [pytest.approx(4.0)]
    assert finished.transient_retries == {}
    by_url = {result["normalized_url"]: result for result in finished.results}
    assert by_url[fast_url]["status"] == "success"
    assert by_url[slow_url]["status"] == "failed"
    assert by_url[slow_url]["classification"] == "url_budget_exceeded"
    assert by_url[slow_url]["error"] == "effect_timeout"


async def test_crawl_claim_surfaces_the_timed_out_outcome() -> None:
    async def _hang() -> None:
        await asyncio.Event().wait()

    claim = await run_crawl_with_claim(
        EffectContainer(concurrency_per_kind={}),
        _hang,
        _URLS[0],
        0.01,
        cast("JobStore", _ClaimStore()),
        "job",
        "token",
    )

    assert claim.outcome is EffectOutcome.TIMED_OUT
