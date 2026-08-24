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


@pytest.fixture(autouse=True)
def fast_sleeps():
    """Patch time.sleep and asyncio.sleep to be instant by default.

    By default the test suite runs with no-op (instant) sleeps so timing
    routines (scheduler delays, retry backoffs, browser lazy-scroll waits)
    do not slow tests down. Tests that need real wall-clock timing must
    explicitly request the ``real_sleeps`` fixture to undo the patch.
    """
    with patch("time.sleep", lambda *a, **k: None), \
         patch("asyncio.sleep", _fast_asyncio_sleep):
        yield


@pytest.fixture
def real_sleeps(monkeypatch):
    """Restore the real time.sleep / asyncio.sleep for timing-sensitive tests.

    Undoes the autouse ``fast_sleeps`` patch via monkeypatch.setattr so the
    original functions are restored for the duration of the requesting test.
    """
    monkeypatch.setattr(time, "sleep", _ORIGINAL_TIME_SLEEP)
    monkeypatch.setattr(asyncio, "sleep", _ORIGINAL_ASYNCIO_SLEEP)
    yield


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
    async with httpx.ASGITransport(app=app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture(autouse=True, scope="session")
def _cleanup_extraction_executors():
    """Shutdown the document extraction process pool at end of test session."""
    yield
    import asyncio
    from app.crawl.extraction import _shutdown_doc_extraction_executor
    asyncio.run(_shutdown_doc_extraction_executor())
    # Also shutdown the thread pool if needed
    from app.crawl.extraction import _EXTRACTION_EXECUTOR
    if _EXTRACTION_EXECUTOR is not None:
        _EXTRACTION_EXECUTOR.shutdown(wait=False)


@pytest.fixture
def base_policy():
    """Fresh minimal crawl policy; tests may mutate their copy freely."""
    from _testkit import make_base_policy
    return make_base_policy()


@pytest.fixture
def job_store(tmp_path):
    """JobStore backed by a per-test SQLite db under ``tmp_path``."""
    from helpers import make_job_store
    return make_job_store(str(tmp_path))


@pytest.fixture
def counting_server():
    """Real HTTP server serving one cacheable page; counts hits on ``server.hits``."""
    from helpers.server import CountingHandler, spawn_threaded_server
    server = spawn_threaded_server(CountingHandler)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def etag_server():
    """Real HTTP server honoring If-None-Match; counts hits on ``server.hits``.

    Mutate ``helpers.server.EtagHandler.etag`` / ``.body`` to simulate an
    upstream content change.
    """
    from helpers.server import EtagHandler, spawn_threaded_server
    server = spawn_threaded_server(EtagHandler)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _close_pooled_http_client():
    """Close the pooled httpx.AsyncClient after each test.

    Tests drive many short-lived ``asyncio.run`` loops; without this the pooled
    client keeps sockets bound to dead loops and pytest's unraisable-warning
    plugin fails a random later test with ResourceWarning.
    """
    yield
    import asyncio
    from app.crawl.strategies.http import close_async_http_client
    try:
        asyncio.run(close_async_http_client())
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


@pytest.fixture(autouse=True)
def _ensure_clean_extraction_pool():
    """Ensure the extraction process pool is clean before each test.

    If test env vars were set by a previous test (e.g., test_extraction_bounds),
    the process pool workers might have inherited them. This fixture resets the
    pool before each test to ensure isolation.
    """
    # Before each test, reset the process pool to ensure clean state
    import app.crawl.extraction as ext_mod
    if ext_mod._DOC_EXTRACTION_EXECUTOR is not None:
        ext_mod._DOC_EXTRACTION_EXECUTOR.shutdown(wait=False)
        ext_mod._DOC_EXTRACTION_EXECUTOR = None
    # Also clear any test env vars that might have leaked
    os.environ.pop("TEST_MARKITDOWN_HANG", None)
    os.environ.pop("TEST_MARKITDOWN_OUTPUT", None)
    yield
