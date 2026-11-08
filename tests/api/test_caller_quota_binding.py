"""#194: keyed quotas are request-validators, not quotas.
Two defects close here: (1) a key's positive ``max_pages`` ceiling must bind an
omitted/zero ``limits`` block, and (2) the sync door must count toward concurrency.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.api.caller_auth import identity_for_key
from crawlutron.api.caller_concurrency import CallerConcurrencyTracker
from crawlutron.config.settings import CrawlerSettings
from crawlutron.storage.caller_key_store import CallerKeyStore
from tests._api_harnesses import keyed_quota_client as _keyedquota_client
from tests._api_harnesses import mount_tracker as _mount_tracker
from tests._api_harnesses import stored_request as _stored_request
from tests._async_harnesses import expect_quota_refused
from tests._policy_factories import job_store as make_store
from tests.factories.client import keyed_quota_crawl, post_crawl, quota_client


def _keyed_headers(key: str) -> dict[str, str]:
    """The auth header for a caller who presents ``key``."""
    return {"X-API-Key": key}


async def _stored_crawl(client, store, *, key: str, max_pages: int = 0):
    """POST one async crawl for ``key`` and return the stored job and response."""
    response = await post_crawl(client, max_pages=max_pages, key=key)
    return _stored_request(store, response), response


async def test_keyed_crawl_stores_the_key_bound_when_limits_omitted(tmp_path):
    """A key declaring ``max_pages=1`` caps an omitted limits block in the stored policy."""
    async with keyed_quota_crawl(tmp_path, key_max_pages=1, body_max_pages=None) as (store, _, _, response):
        stored = _stored_request(store, response)
        assert stored.policy.limits.max_pages == 1


async def test_keyed_crawl_caps_an_explicit_unbounded_request(tmp_path):
    """An explicit ``max_pages=0`` from a keyed caller resolves to the key's bound."""
    async with quota_client(tmp_path) as (client, store, results, keys):
        issued = keys.issue_key("agent-a", max_pages=3)
        stored, _ = await _stored_crawl(client, store, key=issued.secret, max_pages=0)
        assert stored.policy.limits.max_pages == 3


async def test_keyed_crawl_keeps_an_explicit_under_ceiling_limit(tmp_path):
    """A keyed caller who asks for less than the ceiling keeps their own number."""
    async with quota_client(tmp_path) as (client, store, results, keys):
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
    from tests._concurrency_probe import block_sync_crawl, crawl_then_second_refused

    blocker, entered, released = block_sync_crawl(monkeypatch)
    async with _keyedquota_client(tmp_path) as (client, store, tracker, issued):
        headers = _keyed_headers(issued.secret)
        await crawl_then_second_refused(
            client,
            headers=headers,
            entered=entered,
            released=released,
        )


async def test_sync_crawl_releases_the_slot_when_the_engine_fails(tmp_path, monkeypatch):
    """The concurrency slot is released on engine failure, not leaked."""
    from crawlutron.api import crawl_endpoint

    async def _failing_crawl(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(crawl_endpoint, "execute_crawl", _failing_crawl)
    async with _keyedquota_client(tmp_path) as (client, store, tracker, issued):
        headers = _keyed_headers(issued.secret)
        with pytest.raises(RuntimeError):
            await client.post("/v1/crawl", headers=headers, json={"url": "http://example.com/"})
        assert tracker.sync_in_flight(issued.key_id) == 0


async def test_keyed_job_stores_the_key_bound_when_limits_omitted(tmp_path, monkeypatch):
    """The /v1/jobs door stores the key's ceiling, not the unbounded read edge."""
    from crawlutron.api import jobs_router
    from crawlutron.api.caller_auth import Tracking
    from crawlutron.models.job_create_request import JobCreateRequest

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
        tracking=Tracking(job_store=store),
    )

    job = store.load(response.job_id)
    assert job is not None
    assert job.policy.limits.max_pages == 1


async def test_keyed_explicit_max_pages_over_the_ceiling_is_429(tmp_path):
    """A key with ``max_pages=3`` refuses an explicit ``limits.max_pages=100``.
    The code 429s today but was never probed: the explicit request must not
    bypass the key's ceiling (#194).
    """

    async with keyed_quota_crawl(tmp_path, key_max_pages=3, body_max_pages=100) as (_, _, _, response):
        expect_quota_refused(response)


async def test_anonymous_sync_concurrency_is_bounded_at_four(tmp_path, monkeypatch):
    """Five concurrent anonymous sync crawls: four run, the fifth 429s (#194).
    The anonymous max_concurrency is four; only keyed sync bounds were tested.
    """
    from crawlutron.api import crawl_endpoint

    released = asyncio.Event()
    entered = 0

    async def _blocking(*args, **kwargs):
        nonlocal entered
        entered += 1
        await released.wait()
        return {"request_id": "sync", "results": [], "submitted_count": 0, "policy_schema_version": ""}

    monkeypatch.setattr(crawl_endpoint, "execute_crawl", _blocking)
    async with quota_client(tmp_path) as (client, store, results, keys):
        _mount_tracker(client)
        tasks = [
            asyncio.create_task(client.post("/v1/crawl", json={"url": "http://example.com/"}))
            for _ in range(5)
        ]
        # The fifth request 429s at admission and returns while the other four stay parked
        # in the blocker; wait for that first completion, then release the parked four.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        refused = next(iter(done)).result()
        expect_quota_refused(refused)
        assert entered == 4, f"expected 4 anonymous sync crawls admitted, saw {entered}"
        released.set()
        responses = [await t for t in tasks]
        assert [r.status_code for r in responses].count(200) == 4


async def test_sync_crawl_counts_cross_door_with_an_async_job(tmp_path):
    """One keyed async job plus one sync crawl is quota-full at ``max_concurrency=1`` (#194).
    The cross-door sum must bind: an async PENDING row already takes the unkeyed slot.
    """
    async with _keyedquota_client(tmp_path) as (client, store, tracker, issued):
        # A PENDING async row counts toward the key's concurrency without starting a real
        # background crawl that could stray past this test's teardown.
        store.create_single_shot("async_busy", "http://example.com/", {}, owner=issued.key_id)
        assert store.active_count_for_caller(issued.key_id) == 1
        expect_quota_refused(
            await client.post(
                "/v1/crawl", headers=_keyed_headers(issued.secret), json={"url": "http://example.com/"}
            )
        )
