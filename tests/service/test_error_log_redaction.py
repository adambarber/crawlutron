"""Credentialed exception text never reaches a service log line (#1447).

Each sink is driven through its public door; caplog scans the record's own
fields, so a credential hidden in ``extra`` cannot pass as a clean message.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.service import async_crawl_runner, job_runner
from crawlutron.service.policy_suggestions import policy_suggestions
from crawlutron.storage.result_store import CrawlResultStore
from tests._job_drivers import _seed_job
from tests._result_store_fixture import loop_permitted_store

result_store = loop_permitted_store(lambda path: CrawlResultStore(path / "results.db"))

LEAK = "s3cret-value"
USERINFO = "user:pass"
RAW = f"https://{USERINFO}@evil.example/x?token={LEAK}"
ROOT = "http://example.test/root"
POLICY: dict[str, JsonValue] = {"limits": {"max_depth": 0, "max_pages": 0, "respect_robots": False}}


def _leaked(record: logging.LogRecord) -> bool:
    seen = " ".join(str(value) for value in vars(record).values())
    return LEAK in seen or USERINFO in seen


async def test_async_crawl_failed_log_never_emits_a_credentialed_url(job_store, result_store, caplog):
    """A failing crawl's terminal log must redact its error field, keeping the host."""
    job_store.create_single_shot("req-1", ROOT, {"url": ROOT})

    async def failing_crawl(*args, **kwargs):
        raise RuntimeError(RAW)

    with patch("crawlutron.service.async_crawl_runner.execute_crawl", new=failing_crawl):
        with caplog.at_level(logging.ERROR, logger="crawlutron.service.async_crawl_runner"):
            await async_crawl_runner.run_async_crawl(
                "req-1",
                CrawlRequest.model_validate({"url": ROOT}),
                None,
                CrawlerSettings.from_mapping({}),
                job_store,
                result_store,
                asyncio.Event(),
            )
    records = [r for r in caplog.records if r.getMessage() == "async_crawl_failed"]
    assert records
    assert not any(_leaked(r) for r in records)
    assert "evil.example" in " ".join(str(r.error) for r in records)


async def test_job_runner_failed_log_never_emits_a_credentialed_url(job_store, caplog, monkeypatch):
    """The job-runner failure sink logs structured text, never exc_info or %r text."""
    from crawlutron.admission.admission import JobAdmissionContext

    job = _seed_job(job_store, ROOT, POLICY, frontier=[ROOT])

    def boom(*args, **kwargs):
        raise RuntimeError(RAW)

    monkeypatch.setattr(JobAdmissionContext, "create", boom)
    with caplog.at_level(logging.ERROR, logger="crawlutron.service.job_runner"):
        await job_runner.run_job(job.job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event())
    records = [r for r in caplog.records if r.getMessage().startswith("job_runner_failed")]
    assert records
    assert all(r.exc_info is None for r in records)
    assert not any(_leaked(r) for r in records)


async def test_policy_suggestions_skip_log_never_emits_a_credentialed_url(result_store, caplog):
    """The malformed-row skip must redact pydantic's raw validation text."""
    await result_store.aput("crawl_bad", {"request_id": "crawl_bad", "results": RAW})
    with caplog.at_level(logging.WARNING, logger="crawlutron.service.policy_suggestions"):
        response = await policy_suggestions(
            result_store=result_store, settings=CrawlerSettings.from_mapping({})
        )
    assert response.records_skipped == 1
    records = [r for r in caplog.records if r.getMessage() == "policy_suggestions_skip_malformed"]
    assert records
    assert not any(_leaked(r) for r in records)
