"""#194: keyed quotas are request-validators, not quotas.
Two defects close here: (1) a key's positive ``max_pages`` ceiling must bind an
omitted/zero ``limits`` block, and (2) the sync door must count toward concurrency.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.caller_auth import identity_for_key
from app.api.caller_concurrency import CallerConcurrencyTracker
from app.config.settings import CrawlerSettings
from app.storage.caller_key_store import CallerKeyStore
from tests._policy_factories import job_store as make_store
from tests.api.test_caller_isolation import (
    _expect_quota_refused,
    _post_crawl,
    _quota_client,
    _stored_request,
)


def _keyed_headers(key: str) -> dict[str, str]:
    """The auth header for a caller who presents ``key``."""
    return {"X-API-Key": key}


def _mount_tracker(client) -> CallerConcurrencyTracker:
    """Attach the sync-door register to the test client's app and return it."""
    tracker = CallerConcurrencyTracker()
    client._transport.app.state.concurrency_tracker = tracker
    return tracker


async def _stored_crawl(client, store, *, key: str, max_pages: int = 0):
    """POST one async crawl for ``key`` and return the stored job and response."""
    response = await _post_crawl(client, max_pages=max_pages, key=key)
    return _stored_request(store, response), response


async def test_keyed_crawl_stores_the_key_bound_when_limits_omitted(tmp_path):
    """A key declaring ``max_pages=1`` caps an omitted limits block in the stored policy."""
    async with _quota_client(tmp_path) as (client, store, results, keys):
        issued = keys.issue_key("agent-a", max_pages=1)
        response = await client.post(
            "/v1/crawl",
            headers=_keyed_headers(issued.secret),
            json={"url": "http://example.com/", "async_mode": True},
        )
        stored = _stored_request(store, response)
        assert stored.policy.limits.max_pages == 1


async def test_keyed_crawl_caps_an_explicit_unbounded_request(tmp_path):
    """An explicit ``max_pages=0`` from a keyed caller resolves to the key's bound."""
    async with _quota_client(tmp_path) as (client, store, results, keys):
        issued = keys.issue_key("agent-a", max_pages=3)
        stored, _ = await _stored_crawl(client, store, key=issued.secret, max_pages=0)
        assert stored.policy.limits.max_pages == 3


async def test_keyed_crawl_keeps_an_explicit_under_ceiling_limit(tmp_path):
    """A keyed caller who asks for less than the ceiling keeps their own number."""
    async with _quota_client(tmp_path) as (client, store, results, keys):
        issued = keys.issue_key("agent-a", max_pages=10)
        stored, _ = await _stored_crawl(client, store, key=issued.secret, max_pages=2)
        assert stored.policy.limits.max_pages == 2


def test_tracker_reserves_and_releases_per_caller():
    """The sync-door in-flight register is per-caller and drops a caller at zero."""
    tracker = CallerConcurrencyTracker()
    assert tracker.sync_in_flight("key_a") == 0

    tracker.reserve("key_a")
    tracker.reserve("key_a")
    tracker.reserve("key_b")
    assert tracker.sync_in_flight("key_a") == 2
    assert tracker.sync_in_flight("key_b") == 1
    assert tracker.sync_in_flight("key_c") == 0

    tracker.release("key_a")
    tracker.release("key_a")
    tracker.release("key_b")
    assert tracker.sync_in_flight("key_a") == 0
    tracker.release("key_a")
    assert tracker.sync_in_flight("key_a") == 0


async def test_sync_crawl_counts_toward_concurrency(tmp_path, monkeypatch):
    """The second of two concurrent sync crawls is a 429 for ``max_concurrency=1``."""
    from app.api import crawl_endpoint

    entered = asyncio.Event()
    released = asyncio.Event()

    async def _blocking_crawl(*args, **kwargs):
        entered.set()
        await released.wait()
        return {"request_id": "sync", "results": [], "submitted_count": 0, "policy_schema_version": ""}

    monkeypatch.setattr(crawl_endpoint, "execute_crawl", _blocking_crawl)
    async with _quota_client(tmp_path) as (client, store, results, keys):
        _mount_tracker(client)
        issued = keys.issue_key("agent-a", max_concurrency=1)
        headers = _keyed_headers(issued.secret)
        first = asyncio.create_task(
            client.post("/v1/crawl", headers=headers, json={"url": "http://example.com/"})
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            second = await asyncio.wait_for(
                client.post("/v1/crawl", headers=headers, json={"url": "http://example.com/"}),
                timeout=3,
            )
        except TimeoutError:
            released.set()
            await asyncio.wait_for(first, timeout=5)
            raise AssertionError(
                "second concurrent sync crawl was not bounded by max_concurrency=1"
            ) from None
        _expect_quota_refused(second)
        released.set()
        response = await asyncio.wait_for(first, timeout=5)
        assert response.status_code in (200, 201), response.text
        third = await client.post("/v1/crawl", headers=headers, json={"url": "http://example.com/"})
        assert third.status_code in (200, 201), third.text


async def test_sync_crawl_releases_the_slot_when_the_engine_fails(tmp_path, monkeypatch):
    """The concurrency slot is released on engine failure, not leaked."""
    from app.api import crawl_endpoint

    async def _failing_crawl(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(crawl_endpoint, "execute_crawl", _failing_crawl)
    async with _quota_client(tmp_path) as (client, store, results, keys):
        tracker = _mount_tracker(client)
        issued = keys.issue_key("agent-a", max_concurrency=1)
        headers = _keyed_headers(issued.secret)
        with pytest.raises(RuntimeError):
            await client.post("/v1/crawl", headers=headers, json={"url": "http://example.com/"})
        assert tracker.sync_in_flight(issued.key_id) == 0


async def test_keyed_job_stores_the_key_bound_when_limits_omitted(tmp_path, monkeypatch):
    """The /v1/jobs door stores the key's ceiling, not the unbounded read edge."""
    from app.api import jobs_router
    from app.models.job_create_request import JobCreateRequest

    store = make_store(tmp_path / "jobs.db")
    keys = CallerKeyStore(tmp_path / "keys.db")
    record = keys.verify_key(keys.issue_key("agent-a", max_pages=1).secret)
    assert record is not None
    caller = identity_for_key(record)
    monkeypatch.setattr(jobs_router.job_runner, "start_job", lambda *args, **kwargs: None)

    response = await jobs_router.create_job(
        JobCreateRequest(root_url="http://example.com/"),
        job_store=store,
        settings=CrawlerSettings.from_mapping({}),
        shutdown_event=asyncio.Event(),
        caller=caller,
    )

    job = store.load(response.job_id)
    assert job is not None
    assert job.policy.limits.max_pages == 1
