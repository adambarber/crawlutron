"""The caller-isolation API harness: isolated stores behind a real HTTP client."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from crawlutron.api.dependencies import get_job_store, get_result_store, get_settings, get_shutdown_event
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.json_value import JsonValue
from crawlutron.storage import caller_key_store, job_store, result_store
from tests._app_harnesses import harness_app
from tests._async_harnesses import serve
from tests._quota_harness import grant_test_admin_token, keyed_client, stores

# The isolated-stores result both harness clients yield: HTTP client plus the
# store trio, unpacked as ``(client, store, results, keys)`` at call sites.
type _Isolated = tuple[
    AsyncClient,
    job_store.JobStore,
    result_store.CrawlResultStore,
    caller_key_store.CallerKeyStore,
]


def isolation_app(
    store: job_store.JobStore,
    results: result_store.CrawlResultStore,
    keys: caller_key_store.CallerKeyStore,
) -> FastAPI:
    """A test app with the crawl and key routers over isolated stores."""
    from crawlutron.api.caller_concurrency import CallerConcurrencyTracker
    from crawlutron.api.crawl_router import router as crawl_router
    from crawlutron.api.keys_router import router as keys_router

    app = harness_app(crawl_router, keys_router)
    app.state.caller_key_store = keys
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    grant_test_admin_token(app)
    app.dependency_overrides[get_job_store] = lambda: store
    app.dependency_overrides[get_result_store] = lambda: results
    return app


def close_all(*stores_: object) -> None:
    """Close every store harness call sites opened; None entries are skipped."""
    for store_ in stores_:
        close = getattr(store_, "close", None)
        if close is not None:
            close()


@contextlib.asynccontextmanager
async def isolation_client(
    tmp_path: Path,
) -> AsyncGenerator[_Isolated, None]:
    """An HTTP client over isolated stores, yielding the client and the stores."""
    store, results, keys = stores(tmp_path)
    app = isolation_app(store, results, keys)
    async with serve(app) as client:
        try:
            yield client, store, results, keys
        finally:
            close_all(store, results, keys)


@contextlib.asynccontextmanager
async def quota_client(
    tmp_path: Path,
) -> AsyncGenerator[_Isolated, None]:
    """An HTTP client whose app can run the crawl POST: settings and shutdown wired."""
    store, results, keys = stores(tmp_path)
    app = isolation_app(store, results, keys)
    async with keyed_client(
        app,
        keys=keys,
        overrides={
            get_settings: lambda: CrawlerSettings.from_mapping({}),
            get_shutdown_event: asyncio.Event,
        },
    ) as client:
        try:
            yield client, store, results, keys
        finally:
            close_all(store, results, keys)


async def post_crawl(client: AsyncClient, *, max_pages: int | None = 0, key: str | None = None):
    """POST one /v1/crawl for a caller; anonymous when no key is given.

    ``max_pages=None`` omits the limits block entirely -- the shape a key's
    own ceiling must bind (#194)."""
    headers = {"X-API-Key": key} if key else None
    body: dict[str, JsonValue] = {"url": "http://example.com/", "async_mode": True}
    if max_pages is not None:
        body["policy"] = {"limits": {"max_pages": max_pages}}
    return await client.post("/v1/crawl", headers=headers, json=body)


async def keyed_crawl(
    client: AsyncClient,
    keys: caller_key_store.CallerKeyStore,
    *,
    key_max_pages: int,
    body_max_pages: int | None = 0,
    label: str = "agent-a",
):
    """One quota scenario: issue a key bound at ``key_max_pages``, POST as it.

    The arrange triplet (issue key + present secret + crawl POST) stated once;
    the response (and the caller's own assertion about it) stays with the test."""
    issued = keys.issue_key(label, max_pages=key_max_pages)
    response = await post_crawl(client, max_pages=body_max_pages, key=issued.secret)
    return issued, response


def keyed_zero_bound(store: job_store.JobStore, response):
    """The stored policy of a #593 crawl: an explicit zero resolves to the bound."""
    from tests._api_harnesses import stored_request as _stored_request

    return _stored_request(store, response)


@contextlib.asynccontextmanager
async def keyed_quota_crawl(
    tmp_path: Path,
    *,
    key_max_pages: int,
    body_max_pages: int | None = 0,
    label: str = "agent-a",
):
    """The full keyed-quota scenario: a served client, an issued key, one POST.

    Yields ``(store, keys, issued, response)``; the caller asserts on the
    response or the stored row -- the arrange is stated once here."""
    async with quota_client(tmp_path) as (client, store, results, keys):
        issued, response = await keyed_crawl(
            client, keys, key_max_pages=key_max_pages, body_max_pages=body_max_pages, label=label
        )
        yield store, keys, issued, response
