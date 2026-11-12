"""Red-first cross-door probe for #647: REST create must honor the tracker.

A tracker-reserved sync crawl holds the caller's only slot; the REST create door then refuses 429
quota_exceeded because create_job_core sees the same tracker the sync door reserved against.
"""

from __future__ import annotations

import inspect

from tests._api_harnesses import mount_tracker
from tests._concurrency_probe import (
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
        issued = keys.issue_key("agent-a", max_concurrency=1)
        record = keys.verify_key(issued.secret)
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
