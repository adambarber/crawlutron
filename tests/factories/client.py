"""The caller-isolation API harness: isolated stores behind a real HTTP client."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import get_job_store, get_result_store, get_settings, get_shutdown_event
from app.config.settings import CrawlerSettings
from app.storage import caller_key_store, job_store, result_store
from tests._async_harnesses import serve
from tests._quota_harness import keyed_client, stores


def isolation_app(
    store: job_store.JobStore,
    results: result_store.CrawlResultStore,
    keys: caller_key_store.CallerKeyStore,
) -> FastAPI:
    """A test app with the crawl and key routers over isolated stores."""
    from app.api.crawl_router import router as crawl_router
    from app.api.keys_router import router as keys_router

    app = FastAPI()
    app.include_router(crawl_router)
    app.include_router(keys_router)
    app.state.caller_key_store = keys
    app.state.key_admin_token = "test-admin-token"
    app.dependency_overrides[get_job_store] = lambda: store
    app.dependency_overrides[get_result_store] = lambda: results
    return app


@contextlib.asynccontextmanager
async def isolation_client(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[AsyncClient, job_store.JobStore, result_store.CrawlResultStore, caller_key_store.CallerKeyStore]
]:
    """An HTTP client over isolated stores, yielding the client and the stores."""
    store, results, keys = stores(tmp_path)
    app = isolation_app(store, results, keys)
    async with serve(app) as client:
        yield client, store, results, keys


@contextlib.asynccontextmanager
async def quota_client(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[AsyncClient, job_store.JobStore, result_store.CrawlResultStore, caller_key_store.CallerKeyStore]
]:
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
        yield client, store, results, keys


async def post_crawl(client: AsyncClient, *, max_pages: int, key: str | None = None):
    """POST one /v1/crawl for a caller; anonymous when no key is given."""
    headers = {"X-API-Key": key} if key else None
    return await client.post(
        "/v1/crawl",
        headers=headers,
        json={
            "url": "http://example.com/",
            "async_mode": True,
            "policy": {"limits": {"max_pages": max_pages}},
        },
    )
