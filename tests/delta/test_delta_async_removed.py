"""The async crawl runner's removed path: a baseline URL the async run never
answers is reported removed in the persisted delta_summary."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from crawlutron.api.async_crawl_runner import run_async_crawl
from crawlutron.config.settings import CrawlerSettings
from crawlutron.delta import DeltaContext, extract_baseline, extract_baseline_entries
from crawlutron.models.crawl_request import CrawlRequest
from tests._job_harnesses import make_job_store
from tests._result_builders import engine_result
from tests.delta._door_harness import assert_removed_summary, delta_stores

URL = "http://example.com/"
REMOVED_URL = "http://example.com/gone/"


def _stores(tmpdir: str):
    return delta_stores(tmpdir)


async def test_async_run_reports_baseline_url_absent_from_run(monkeypatch, tmp_path) -> None:
    tmpdir = str(tmp_path)
    result_store, spill_store = _stores(tmpdir)
    stored: dict[str, object] = {
        "request_id": "baseline-1",
        "results": [
            {
                "original_url": URL,
                "normalized_url": URL,
                "validators": {"etag": '"v1"'},
                "markdown": "one",
            },
            {
                "original_url": REMOVED_URL,
                "normalized_url": REMOVED_URL,
                "validators": {"etag": '"v1"'},
                "markdown": "two",
            },
        ],
    }
    baseline = DeltaContext(
        changed_since=None,
        baseline_request_id="baseline-1",
        baseline=extract_baseline(stored),
        baseline_expired=False,
        entries=extract_baseline_entries(stored),
    )
    scripted = AsyncMock(return_value={"request_id": "r1", "results": [_result()], "submitted_count": 1})
    payload = CrawlRequest(url=URL)
    job_store = make_job_store(tmpdir)
    await job_store.acreate_single_shot("r1", URL, payload.policy, owner="caller-a")
    job = await job_store.aload("r1")
    with patch("crawlutron.api.async_crawl_runner.execute_crawl", new=scripted):
        with patch("crawlutron.service.webhooks.post_webhook", new_callable=AsyncMock):
            await run_async_crawl(
                "r1",
                payload,
                None,
                CrawlerSettings.from_mapping(),
                job_store,
                result_store,
                asyncio.Event(),
                url_pairs=None,
                spill_store=spill_store,
                delta_context=baseline,
            )
    job = await job_store.aload("r1")
    assert job is not None and job.results is not None
    payload_row = job.results[0]
    assert isinstance(payload_row, dict)
    summary = payload_row["delta_summary"]
    assert isinstance(summary, dict)
    assert_removed_summary(summary, REMOVED_URL)


def _result():
    return engine_result(URL, "not_modified", http_status=304, markdown="one", validators={"etag": '"v1"'})
