"""Common test utilities to reduce duplication across test modules."""

import asyncio
import pathlib
from typing import Any, Callable, Awaitable

import httpx
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.crawl.job_store import JobStore
from app.constants import HTTP_DECLARED_CRAWLER


def build_payload(
    *,
    url: str | None = None,
    urls: list[str] | None = None,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    **extra,
) -> dict:
    """Build a crawl request payload dict from keyword arguments."""
    payload = {}
    if url:
        payload["url"] = url
    if urls:
        payload["urls"] = urls
    if strategies is not None:
        payload["policy"] = {"strategies": strategies}
    if limits is not None:
        payload.setdefault("policy", {})["limits"] = limits
    payload.update(extra)
    return payload


# Shared HTML fixtures to avoid duplication across test modules.
FIRECRAWL_TEST_HTML = (
    "<html lang='en'><head><title>Test Page</title>"
    "<meta content='no-name-attribute'>"
    "<meta name='description' content=''>"
    "<meta name='description' content='A test page'>"
    "<meta property='og:title' content='OG Title'>"
    "</head><body><main>Content here</main></body></html>"
)


# Async helper for pytest-based tests
async def run_crawl_async(payload: dict, *, cache=None, browser_worker=None) -> dict:
    """Async version of run_crawl for pytest-asyncio tests."""
    if cache is None:
        cache = HttpCache()
    return await crawl_request(payload, browser_worker=browser_worker, cache=cache)


async def crawl_homepage_async(
    base_url: str,
    *,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    cache=None,
    browser_worker=None,
) -> dict:
    """Crawl the local test server homepage and return the unified response dict."""
    payload = build_payload(
        url=f"{base_url}/",
        strategies=strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
        limits=limits if limits is not None else {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    )
    return await run_crawl_async(payload, cache=cache, browser_worker=browser_worker)


# --- New helpers to reduce test duplication ---


def make_async_client(app) -> Callable[[], Awaitable[httpx.AsyncClient]]:
    """Create a factory for async test clients.

    Usage:
        async with make_async_client(app)() as client:
            resp = await client.post("/v1/endpoint", json={...})

    This avoids repeating the httpx.ASGITransport + AsyncClient boilerplate.
    """
    def _factory() -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")
    return _factory


async def run_async_test(
    app,
    test_fn: Callable[[httpx.AsyncClient], Awaitable[None]],
) -> None:
    """Run an async test function with a test client.

    Usage:
        async def test():
            async with make_async_client(app)() as client:
                resp = await client.post("/v1/endpoint", json={...})
                assert resp.status_code == 200
        run_async_test(app, test)

    Or more simply:
        await run_async_test(app, lambda client: client.post(...))
    """
    async with make_async_client(app)() as client:
        await test_fn(client)


def make_content_cache(tmpdir: str, *, default_ttl: float = 1, clock=None) -> ContentCache:
    """Create a ContentCache with test-friendly defaults.

    Args:
        tmpdir: Temporary directory path
        default_ttl: Default TTL in seconds (default 1)
        clock: Optional clock function for time control (default: time.monotonic)

    Returns:
        Configured ContentCache instance
    """
    if clock is None:
        import time
        clock = time.monotonic
    return ContentCache(tmpdir, default_ttl=default_ttl, clock=clock)


def make_job_store(tmpdir: str) -> JobStore:
    """Create a JobStore with test-friendly defaults.

    Args:
        tmpdir: Temporary directory path

    Returns:
        Configured JobStore instance
    """
    db_path = pathlib.Path(tmpdir) / "jobs.db"
    return JobStore(db_path)


async def run_job_control_test(
    app,
    job_store: JobStore,
    job_id: str,
    action: str,
    expected_status: int = 422,
) -> None:
    """Run a job control test with the given action and expected status.

    This helper encapsulates the common pattern for testing job control endpoints.

    Usage:
        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "cancel", 422)
    """
    from httpx import AsyncClient
    import httpx

    async with make_async_client(app)() as client:
        resp = await client.post(
            f"/v1/jobs/{job_id}/control",
            json={"action": action}
        )
        assert resp.status_code == expected_status

def make_content_cache_with_entry(tmpdir: str, key: str = "key1", value: dict = None, *, default_ttl: float = 1, clock=None) -> ContentCache:
    """Create a ContentCache with a test entry already set.

    This reduces duplication in ContentCache tests that all start with
    the same setup pattern.

    Args:
        tmpdir: Temporary directory path
        key: Cache key to set (default: "key1")
        value: Value to store (default: {"data": "1"})
        default_ttl: Default TTL in seconds (default 1)
        clock: Optional clock function for time control

    Returns:
        Configured ContentCache instance with the entry already set
    """
    if value is None:
        value = {"data": "1"}
    cache = make_content_cache(tmpdir, default_ttl=default_ttl, clock=clock)
    cache.set(key, value)
    return cache




def browser_available() -> bool:
    """Return True if a Playwright Chromium binary is installed and can be used.

    Used to skip browser-dependent tests in environments where the browser
    binary is not installed (e.g. lightweight CI images). When the binary is
    present the tests run normally.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    import os
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return bool(path) and os.path.exists(path)
    except Exception:
        return False
