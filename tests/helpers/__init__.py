"""Common test utilities to reduce duplication across test modules."""

import asyncio
import unittest
from typing import Any

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.constants import HTTP_DECLARED_CRAWLER


class ServerTestCase(unittest.TestCase):
    """Base test case providing a real HTTP server (no mocks).

    Kept as a unittest.TestCase for backward compatibility with existing
    unittest-based tests. New tests should use pytest fixtures from conftest.py
    (base_url, make_crawl_payload, etc.) instead.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from helpers.server import spawn_server
        cls.server = spawn_server()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def make_payload(
        self,
        *,
        url: str | None = None,
        urls: list[str] | None = None,
        strategies: list[str] | None = None,
        limits: dict | None = None,
        **extra,
    ) -> dict:
        """Build a crawl request payload with sensible defaults."""
        if url is None and urls is None:
            url = f"{self.base}/"
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
