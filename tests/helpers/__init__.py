"""Common test utilities to reduce duplication across test modules."""

import asyncio
import unittest
from typing import Any

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
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


class ServerTestCase(unittest.TestCase):
    """Base test case providing a real HTTP server (no mocks).

    Kept as a unittest.TestCase for backward compatibility with existing
    unittest-based tests. New tests should use pytest fixtures from conftest.py
    (base_url) and helpers from helpers/__init__.py (run_crawl_async,
    crawl_homepage_async, build_payload) instead.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from helpers.server import spawn_server
        cls.server = spawn_server()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def run_crawl(self, payload: dict, *, cache=None, browser_worker=None) -> dict:
        """Execute crawl_request with test-friendly defaults."""
        if cache is None:
            cache = HttpCache()
        return asyncio.run(crawl_request(payload, browser_worker=browser_worker, cache=cache))

    def assert_crawl_ok(self, response: dict) -> None:
        """Assert a successful crawl response."""
        assert response["submitted_count"] == 1
        assert len(response["results"]) == 1
        assert response["results"][0]["status"] == "success"

    def assert_crawl_failed(self, response: dict, classification: str | None = None) -> None:
        """Assert a failed crawl response."""
        assert response["submitted_count"] == 1
        assert response["results"][0]["status"] == "failed"
        if classification:
            assert response["results"][0]["classification"] == classification


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
