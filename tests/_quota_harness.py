"""One keyed-service test client: a per-request register, key admin, and one browser.

The map, render, MCP, and search quota tests all speak HTTP in-process to an isolated
app carrying the caller register and the public keys door; that plumbing lives here once."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI
from httpx import AsyncClient

from crawlutron.api.dependencies import get_job_store, get_result_store, get_settings, get_shutdown_event
from crawlutron.config.settings import CrawlerSettings
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.storage.caller_key_store import CallerKeyStore
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.result_store import CrawlResultStore
from tests._app_harnesses import harness_app
from tests._async_harnesses import serve
from tests._policy_factories import job_store as make_store

if TYPE_CHECKING:
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


def keyed_overrides(
    app: FastAPI,
    keys: CallerKeyStore,
    *,
    job_store: JobStore | None = None,
    result_store: CrawlResultStore | None = None,
) -> dict[Callable[[Request], object], Callable[[], object]]:
    """The keyed-app wiring: tracker, key store, admin token, store overrides.

    ``job_store``/``result_store`` are optional because a keyed door may serve
    routes that need only the key plane; each named store is wired when given."""
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    app.state.caller_key_store = keys
    grant_test_admin_token(app)
    overrides: dict[Callable[[Request], object], Callable[[], object]] = {
        get_settings: lambda: CrawlerSettings.from_mapping({}),
        get_shutdown_event: asyncio.Event,
    }
    if job_store is not None:
        overrides[get_job_store] = lambda: job_store
    if result_store is not None:
        overrides[get_result_store] = lambda: result_store
    return overrides


@contextlib.asynccontextmanager
async def keyed_client(
    app: FastAPI,
    *,
    keys: CallerKeyStore,
    overrides: Mapping[Callable[[Request], object], Callable[[], object]] | None = None,
) -> AsyncGenerator[AsyncClient, None]:
    """Configure ``app`` as a keyed service and serve it with an in-process client."""
    for dependency, factory in (
        keyed_overrides(app, keys) if overrides is None else {**keyed_overrides(app, keys), **overrides}
    ).items():
        app.dependency_overrides[dependency] = factory
    async with serve(app) as client:
        yield client


@contextlib.asynccontextmanager
async def service_client(
    tmp_path: Path,
    *,
    routers: Mapping[str, APIRouter],
    with_results: bool = False,
    wire: Callable[[FastAPI], None] | None = None,
) -> AsyncGenerator[tuple[AsyncClient, JobStore, CallerKeyStore], None]:
    """Serve a keyed HTTP client over one register and one key admin and ``routers``.

    ``with_results`` also wires a result store and propagates the job store to
    ``app.state`` for the MCP door. ``wire`` runs once the app is mounted, so a
    test substitutes app-wiring state (e.g. the admission port) at the same
    injection point production uses. Yields the client, job store, and key store."""
    job_store, results, keys = stores(tmp_path)
    app = harness_app(*routers.values())
    app.state.job_store = job_store
    overrides = keyed_overrides(app, keys, job_store=job_store)
    if with_results:
        app.state.result_store = results
        overrides[get_result_store] = lambda: results
    if wire is not None:
        wire(app)
    async with keyed_client(app, keys=keys, overrides=overrides) as client:
        try:
            yield client, job_store, keys
        finally:
            job_store.close()
            results.close()
            keys.close()


def mcp_crawl_keys_routers() -> dict[str, APIRouter]:
    """The /mcp + /v1/crawl + /v1/keys router map the cross-door suites serve."""
    from crawlutron.api.crawl_router import router as crawl_router
    from crawlutron.api.keys_router import router as keys_router
    from crawlutron.api.mcp_router import router as mcp_router

    return {"mcp": mcp_router, "crawl": crawl_router, "keys": keys_router}


@contextlib.asynccontextmanager
async def keyed_result_client(
    tmp_path: Path,
    routers: tuple[APIRouter, ...],
) -> AsyncGenerator[tuple[AsyncClient, CrawlResultStore, CallerKeyStore], None]:
    """A keyed client over ``routers`` with a result store the rollup reads.

    Own settings/shutdown/result overrides so the door reads the store handed
    to it, and stashes the store on app.state for the MCP door; yields the
    client, result store, and key store."""
    _, results, keys = stores(tmp_path)
    app = harness_app(*routers)
    app.state.result_store = results
    async with keyed_client(
        app,
        keys=keys,
        overrides={
            get_result_store: lambda: results,
            get_settings: lambda: CrawlerSettings.from_mapping({}),
            get_shutdown_event: asyncio.Event,
        },
    ) as client:
        try:
            yield client, results, keys
        finally:
            results.close()
            keys.close()
