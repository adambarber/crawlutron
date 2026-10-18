"""One keyed-service test client: a per-request register, key admin, and one browser.

The map, render, MCP, and search quota tests all speak HTTP in-process to an isolated
app carrying the caller register and the public keys door; that plumbing lives here once."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from app.api.caller_concurrency import CallerConcurrencyTracker
from app.storage.caller_key_store import CallerKeyStore
from app.storage.job_store import JobStore
from app.storage.result_store import CrawlResultStore
from tests._async_harnesses import serve
from tests._policy_factories import job_store as make_store


def stores(tmp_path: Path) -> tuple[JobStore, CrawlResultStore, CallerKeyStore]:
    """One isolated triple per test: jobs, results, and keys on scratch files."""
    return (
        make_store(tmp_path / "jobs.db"),
        CrawlResultStore(tmp_path / "results.db"),
        CallerKeyStore(tmp_path / "keys.db"),
    )


@contextlib.asynccontextmanager
async def keyed_client(
    app: FastAPI,
    *,
    keys: CallerKeyStore,
    overrides: Mapping[object, object] | None = None,
) -> AsyncIterator[AsyncClient]:
    """Configure ``app`` as a keyed service and serve it with an in-process client."""
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    app.state.caller_key_store = keys
    app.state.key_admin_token = "test-admin-token"
    for dependency, factory in (overrides or {}).items():
        app.dependency_overrides[dependency] = factory
    async with serve(app) as client:
        yield client


@contextlib.asynccontextmanager
async def service_client(
    tmp_path: Path,
    *,
    routers: Mapping[str, object],
    with_results: bool = False,
) -> AsyncIterator[tuple[AsyncClient, JobStore, CallerKeyStore]]:
    """Serve a keyed HTTP client over one register and one key admin and ``routers``.

    ``with_results`` also wires a result store and propagates the job store to
    ``app.state`` for the MCP door. Yields the client, job store, and key store."""
    from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
    from app.config.settings import CrawlerSettings

    job_store, results, keys = stores(tmp_path)
    app = FastAPI()
    for router in routers.values():
        app.include_router(router)
    app.state.job_store = job_store
    overrides = {
        get_job_store: lambda: job_store,
        get_settings: lambda: CrawlerSettings.from_mapping({}),
        get_shutdown_event: asyncio.Event,
    }
    if with_results:
        from app.api.dependencies import get_result_store

        app.state.result_store = results
        overrides[get_result_store] = lambda: results
    async with keyed_client(app, keys=keys, overrides=overrides) as client:
        yield client, job_store, keys
