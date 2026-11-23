"""Red-first cross-door probe for #647: REST create must honor the tracker.

A tracker-reserved sync crawl holds the caller's only slot; the REST create door then refuses 429
quota_exceeded because create_job_core sees the same tracker the sync door reserved against.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from tests._api_harnesses import mount_tracker
from tests._concurrency_probe import (
    assert_second_concurrent_refused,
    block_sync_crawl,
    crawl_then_second_refused,
)
from tests.factories.client import quota_client


async def test_rest_create_refuses_when_the_tracker_holds_the_callers_slot(tmp_path, monkeypatch):
    """A tracker-reserved sync crawl exhausts the caller's slot; POST /v1/jobs 429s.

    The core's non-divergence promise is cross-door: the REST create must see
    the same concurrency register the sync crawl reserved against."""
    from crawlutron_rest.api.caller_auth import get_caller_identity, identity_for_key
    from crawlutron_rest.api.jobs_router import router as jobs_router

    blocker, entered, released = block_sync_crawl(monkeypatch)
    async with quota_client(tmp_path) as (client, store, results, keys):
        jobs_router_route_app = getattr(client._transport, "app", None)
        assert jobs_router_route_app is not None, "the quota client must ride an ASGI transport"
        jobs_router_route_app.include_router(jobs_router)
        mount_tracker(client)
        issued = await keys.aissue_key("agent-a", max_concurrency=1)
        record = await keys.averify_key(issued.secret)
        assert record is not None
        caller = identity_for_key(record)
        jobs_router_route_app.dependency_overrides[get_caller_identity] = lambda: caller
        headers = {"X-API-Key": issued.secret}
        await crawl_then_second_refused(
            client,
            headers=headers,
            entered=entered,
            released=released,
            second_url="/v1/jobs",
            second_body={"root_url": "http://example.com/"},
        )


def test_create_job_core_requires_a_concurrency_tracker():
    """The core's non-divergence promise is the signature: no tracker, no call."""
    from crawlutron.service.job_cores import create_job_core

    parameters = inspect.signature(create_job_core).parameters
    tracker = parameters["concurrency_tracker"]
    assert tracker.kind is inspect.Parameter.KEYWORD_ONLY
    assert tracker.default is inspect.Parameter.empty


async def test_second_refused_assertion_failure_still_releases_first():
    """A failed second-request assertion still sets ``released`` (try/finally).
    ``entered`` is set inside fake_post's first branch so the probe's
    ``entered.wait()`` guarantees the first call is parked before ``second`` is issued.
    """
    released = asyncio.Event()
    entered = asyncio.Event()
    calls = {"n": 0}

    def make_response(is_first: bool) -> object:
        return type(
            "R",
            (),
            {
                "status_code": 200 if is_first else 500,
                "text": "ok" if is_first else "not 429",
            },
        )()

    async def fake_post(_client, target, *, headers=None, json=None):
        calls["n"] += 1
        is_first = calls["n"] == 1
        if is_first:
            entered.set()  # probe's entered.wait() now syncs before the first call parks
            await released.wait()  # first task parks until released
        return make_response(is_first)

    client = type("C", (), {"post": fake_post})()

    async def _issue(target, payload):
        return await client.post(target, headers={}, json=payload)

    first = asyncio.create_task(_issue("/v1/crawl", {"url": "http://example.com/"}))

    try:
        await assert_second_concurrent_refused(
            client,
            url="/v1/crawl",
            headers=None,
            body={"url": "http://example.com/"},
            entered=entered,
            released=released,
            first_ok=(200, 201),
        )
        pytest.fail("expected AssertionError from the second-request assertion")
    except AssertionError:
        pass

    assert released.is_set(), "released must be set even when the second-request assertion fails"
    response = await asyncio.wait_for(first, timeout=5)
    assert response.status_code == 200
