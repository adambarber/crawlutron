"""Async crawl runner lease/heartbeat behavior."""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.service import async_crawl_runner
from crawlutron.storage.job_store import JobStatus
from crawlutron.storage.safe_db import allow_on_loop
from tests._job_harnesses import make_job_store
from tests._policy_factories import canonical


class _ResultStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, dict[str, object], str]] = []

    async def aput(self, request_id: str, payload: dict[str, object], *, owner: str = "") -> None:
        self.puts.append((request_id, payload, owner))


def _store(tmp_path):
    return make_job_store(tmp_path)


async def _immediate_timeout(awaitable, timeout):
    if hasattr(awaitable, "close"):
        awaitable.close()
    raise TimeoutError


def _lose_lease_once(store, original_renew):
    calls = {"n": 0}

    def renew_then_lose(job_id: str, token: str, *, now: float) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            with allow_on_loop():
                original_renew(job_id, token, now=now)
                assert store.release_job_claim(job_id, token) is True
                assert store.claim_running(job_id, "foreign", now=now, claim_seconds=300.0) is True
            return False
        return original_renew(job_id, token, now=now)

    async def arenew(job_id: str, token: str, *, now: float) -> bool:
        return renew_then_lose(job_id, token, now=now)

    return renew_then_lose, arenew


@pytest.mark.wall_assertion
async def test_renew_failure_aborts_inflight_crawl_without_foreign_clobber(tmp_path, monkeypatch):
    """A lost lease must stop in-flight work and never publish as owner."""
    store = _store(tmp_path)
    request_id = "crawl_async_lease_lost"
    job = store.create_single_shot(request_id, "http://example.com/", {}, owner="caller")
    assert job.status is JobStatus.PENDING
    aborted = asyncio.Event()

    async def fake_execute(*args, **kwargs):
        event = kwargs.get("shutdown_event")
        assert event is not None
        await event.wait()
        aborted.set()
        return {"request_id": request_id, "results": [], "submitted_count": 0, "policy_schema_version": "v1"}

    monkeypatch.setattr(async_crawl_runner, "execute_crawl", fake_execute)
    renew_then_lose, arenew = _lose_lease_once(store, store.renew_job_claim)
    monkeypatch.setattr(store, "renew_job_claim", renew_then_lose)
    monkeypatch.setattr(store, "arenew_job_claim", arenew)
    monkeypatch.setattr(async_crawl_runner.asyncio, "wait_for", _immediate_timeout)
    result_store = _ResultStore()
    await async_crawl_runner.run_async_crawl(
        request_id,
        CrawlRequest(urls=["http://example.com/"]),
        None,
        CrawlerSettings.from_mapping({}),
        store,
        result_store,
        asyncio.Event(),
        url_pairs=[("http://example.com/", canonical("http://example.com/"))],
    )
    assert aborted.is_set(), "in-flight crawl must be aborted on renew failure"
    assert result_store.puts == []
    reloaded = store.load(request_id)
    assert reloaded is not None and reloaded.status is JobStatus.RUNNING
    assert reloaded.claimed_by == "foreign"
    store.close()
