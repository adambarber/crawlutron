"""Shared pytest fixtures and path setup for the browser-crawler test suite.

No per-file ``sys.path`` hacks: this root conftest adds the crawler package
and its shared logging helper to ``sys.path`` once, so every test file can
import :mod:`app` and :mod:`helpers` directly.
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # docker/browser-crawler
SHARED = ROOT.parent / "shared" / "python"

for candidate in (str(ROOT), str(SHARED), str(HERE)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = os.environ.get(
    "BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1"
)
os.environ["BROWSER_CRAWLER_LOG_FILE"] = os.environ.get(
    "BROWSER_CRAWLER_LOG_FILE", "/tmp/browser_crawler_test.log"
)
os.environ["BROWSER_CRAWLER_CACHE_DIR"] = os.environ.get(
    "BROWSER_CRAWLER_CACHE_DIR", "/tmp/browser_crawler_test_cache"
)
os.environ["BROWSER_CRAWLER_CACHE_TTL_SECONDS"] = os.environ.get(
    "BROWSER_CRAWLER_CACHE_TTL_SECONDS", "3600"
)
# Keep the opt-in content cache disabled during tests so fake-fetcher suites that
# serve different content for the same URL stay isolated (#5).
os.environ["BROWSER_CRAWLER_CONTENT_CACHE_ENABLED"] = os.environ.get(
    "BROWSER_CRAWLER_CONTENT_CACHE_ENABLED", "false"
)

# Save original sleep functions before any patching
_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep
_ORIGINAL_TIME_SLEEP = time.sleep

# Provide a no-op async sleep
async def _fast_asyncio_sleep(*args, **kwargs):
    """No-op async sleep for tests."""
    return None


@pytest.fixture(autouse=True, scope="session")
def _mock_sleeps():
    """Patch time.sleep and asyncio.sleep to return instantly for all tests.

    This eliminates real-time waits for:
    - Scheduler jitter and cooldowns
    - Browser lazy-scroll delays
    - Retry backoffs
    - Challenge wait loops
    """
    with patch("time.sleep", lambda *a, **k: None), \
         patch("asyncio.sleep", _fast_asyncio_sleep):
        yield


@pytest.fixture
def real_sleeps():
    """Restore real asyncio.sleep for timing-sensitive tests."""
    original = asyncio.sleep
    asyncio.sleep = _ORIGINAL_ASYNCIO_SLEEP
    time.sleep = _ORIGINAL_TIME_SLEEP
    yield
    asyncio.sleep = original
    time.sleep = _ORIGINAL_TIME_SLEEP


@pytest.fixture(scope="session")
def test_server():
    """Start a real in-process HTTP server once per test session."""
    from helpers.server import spawn_server
    server = spawn_server()
    yield server
    server.stop()


@pytest.fixture
def base_url(test_server):
    """The base URL of the session-scoped test server."""
    return test_server.base_url


@pytest.fixture(autouse=True, scope="session")
def _disable_lazy_scroll():
    """Set lazy scroll delay to 0 for browser tests."""
    import app.crawl.strategies.browser as browser_module
    original_delay = browser_module.LAZY_SCROLL_DELAY_SECONDS
    browser_module.LAZY_SCROLL_DELAY_SECONDS = 0
    yield
    browser_module.LAZY_SCROLL_DELAY_SECONDS = original_delay





@pytest.fixture
async def async_client():
    """Async HTTP client using ASGI transport for FastAPI app."""
    import httpx
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
