"""The typed concurrency-tracker dependency: the app-state contract is load-bearing. A served app that forgot
``app.state.concurrency_tracker`` must be refused loudly at the door (500, tracker_missing), never
silently degraded to job-store-rows-only quota.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from httpx import AsyncClient, Response

from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.errors import DomainError
from crawlutron.storage import caller_key_store, job_store, result_store
from crawlutron_rest.api.dependencies import get_concurrency_tracker
from tests._app_harnesses import harness_app
from tests._async_harnesses import issue_key, serve
from tests._quota_harness import grant_test_admin_token, stores

type _Harness = tuple[
    AsyncClient,
    FastAPI,
    job_store.JobStore,
    result_store.CrawlResultStore,
    caller_key_store.CallerKeyStore,
]


@pytest.fixture
async def trackerless_app(tmp_path: Path) -> AsyncGenerator[_Harness, None]:
    """The crawl router served WITHOUT a concurrency tracker on app.state."""
    from crawlutron_rest.api.crawl_router import router as crawl_router
    from crawlutron_rest.api.keys_router import router as keys_router

    store, results, keys = stores(tmp_path)
    app = harness_app(crawl_router, keys_router)
    app.state.caller_key_store = keys
    app.state.job_store = store
    app.state.result_store = results
    app.state.shutdown_event = asyncio.Event()
    grant_test_admin_token(app)
    async with serve(app) as client:
        yield client, app, store, results, keys


async def _keyed_crawl(client: AsyncClient, *, max_pages: int | None = None) -> Response:
    """Issue one key, then POST /v1/crawl with its secret (max_pages None omits it)."""
    from tests.factories.client import post_crawl

    secret = await issue_key(client)
    return await post_crawl(client, max_pages=max_pages, key=secret)


async def test_crawl_without_tracker_is_refused_not_degraded(trackerless_app):
    client, _, _, _, _ = trackerless_app
    response = await _keyed_crawl(client)
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "tracker_missing"


async def test_crawl_with_tracker_serves(trackerless_app):
    client, app, _, _, _ = trackerless_app
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    response = await _keyed_crawl(client, max_pages=0)
    assert response.status_code in (200, 202), response.text


def test_dependency_refuses_untracked_state():
    """The typed dependency: a bare app.state refuses via the error envelope."""
    scope = {"type": "http", "app": FastAPI()}
    request = Request(scope)
    with pytest.raises(DomainError) as raised:
        get_concurrency_tracker(request)
    assert raised.value.status == 500
    assert raised.value.code == "tracker_missing"
