"""One keyed-service test client: a per-request register, key admin, and one browser.

The map, render, MCP, and search quota tests all speak HTTP in-process to an isolated
app carrying the caller register and the public keys door; that plumbing lives here once."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI
from httpx import AsyncClient

from crawlutron.api.caller_concurrency import CallerConcurrencyTracker
from crawlutron.api.dependencies import get_job_store, get_result_store, get_settings, get_shutdown_event
from crawlutron.config.settings import CrawlerSettings
from crawlutron.storage.caller_key_store import CallerKeyStore
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.result_store import CrawlResultStore
from tests._async_harnesses import serve
from tests._policy_factories import job_store as make_store

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request


def stores(tmp_path: Path) -> tuple[JobStore, CrawlResultStore, CallerKeyStore]:
    """One isolated triple per test: jobs, results, and keys on scratch files."""
    return (
        make_store(tmp_path / "jobs.db"),
        CrawlResultStore(tmp_path / "results.db"),
        CallerKeyStore(tmp_path / "keys.db"),
    )


TEST_ADMIN_TOKEN = "test-admin-token"


def grant_test_admin_token(app: FastAPI, token: str = TEST_ADMIN_TOKEN) -> None:
    """Wire the ONE operator-token home (the settings object) for a test app."""
    app.state.crawler_settings = CrawlerSettings.from_mapping({"CRAWLUTRON_KEY_ADMIN_TOKEN": token})


@contextlib.asynccontextmanager
async def keyed_client(
    app: FastAPI,
    *,
    keys: CallerKeyStore,
    overrides: Mapping[Callable[[Request], object], Callable[[], object]] | None = None,
) -> AsyncGenerator[AsyncClient, None]:
    """Configure ``app`` as a keyed service and serve it with an in-process client."""
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    app.state.caller_key_store = keys
    grant_test_admin_token(app)
    for dependency, factory in (overrides or {}).items():
        app.dependency_overrides[dependency] = factory
    async with serve(app) as client:
        yield client


@contextlib.asynccontextmanager
async def service_client(
    tmp_path: Path,
    *,
    routers: Mapping[str, APIRouter],
    with_results: bool = False,
) -> AsyncGenerator[tuple[AsyncClient, JobStore, CallerKeyStore], None]:
    """Serve a keyed HTTP client over one register and one key admin and ``routers``.

    ``with_results`` also wires a result store and propagates the job store to
    ``app.state`` for the MCP door. Yields the client, job store, and key store."""
    job_store, results, keys = stores(tmp_path)
    app = FastAPI()
    for router in routers.values():
        app.include_router(router)
    app.state.job_store = job_store
    overrides: dict[Callable[[Request], object], Callable[[], object]] = {
        get_job_store: lambda: job_store,
        get_settings: lambda: CrawlerSettings.from_mapping({}),
        get_shutdown_event: asyncio.Event,
    }
    if with_results:
        app.state.result_store = results
        overrides[get_result_store] = lambda: results
    async with keyed_client(app, keys=keys, overrides=overrides) as client:
        yield client, job_store, keys
