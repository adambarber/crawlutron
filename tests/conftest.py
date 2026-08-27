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
# Per-xdist-worker paths: when running under pytest-xdist each worker gets its
# own log/cache files so parallel workers cannot clobber shared state.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
_WORKER_SUFFIX = f"-{_XDIST_WORKER}" if _XDIST_WORKER else ""
os.environ.setdefault(
    "BROWSER_CRAWLER_LOG_FILE", f"/tmp/browser_crawler_test{_WORKER_SUFFIX}.log"
)
os.environ.setdefault(
    "BROWSER_CRAWLER_CACHE_DIR", f"/tmp/browser_crawler_test_cache{_WORKER_SUFFIX}"
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


def pytest_collection_modifyitems(config, items):
    """Auto-skip browser-marked tests when no real browser is installed.

    Keeps ``pytest -m browser`` (and the full run) green on machines without
    Camoufox/Playwright instead of failing mid-suite. When a browser is present
    the marker is left intact so the tests actually execute.
    """
    from helpers import browser_available

    if browser_available():
        return
    skip_no_browser = pytest.mark.skip(reason="browser not available in this environment")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip_no_browser)


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
def _reset_sitemap_cache():
    """Clear the module-level sitemap TTL cache between tests.

    Tests inject fake fetchers serving different content for the same
    sitemap URLs; without this the shared cache leaks across tests.
    """
    import app.crawl.fetch_helpers as fh
    fh._SITEMAP_CACHE.clear()
    yield
    fh._SITEMAP_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_global_http_state():
    """Reset crawler global singletons after each test.

    Tests drive many short-lived ``asyncio.run`` loops; without this the pooled
    httpx client keeps sockets bound to dead loops (pytest's unraisable-warning
    plugin then fails a random later test with ResourceWarning), the global
    cookie jar leaks Set-Cookie state, and the shared HTTP cache / admission
    gate leak crawl state across tests. Resetting here keeps tests
    order-independent and is a prerequisite for pytest-xdist safety.

    The http client close is loop-aware: if an event loop is already running
    (async tests) we drop the reference and let GC reclaim it rather than
    calling ``asyncio.run`` from inside a running loop (which raises and was
    previously swallowed, leaving the client un-reset).
    """
    yield
    import asyncio
    from app.crawl.strategies import http as http_mod
    try:
        http_mod._GLOBAL_COOKIE_JAR.clear()
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass
    try:
        if http_mod._DEFAULT_HTTP_CLIENT is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Inside an async test's loop: drop the reference so GC
                    # reclaims the client instead of awaiting across loops.
                    http_mod._DEFAULT_HTTP_CLIENT = None
                else:
                    loop.run_until_complete(http_mod.close_async_http_client())
            except RuntimeError:
                http_mod._DEFAULT_HTTP_CLIENT = None
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass
    # robots_cache was promoted to module-level and shared across crawl_request
    # calls (#9). Tests reuse origins with different robots.txt bodies, so
    # clear it between tests for isolation. Production keeps it shared (a given
    # origin always serves the same robots.txt).
    try:
        from app.crawl import engine as _engine_mod
        _engine_mod.robots_cache.clear()
        # Fresh shared cache + admission gate so crawl state never leaks across
        # tests (the gate caches its CPU-quota reading and CPU sampling state).
        _engine_mod._SHARED_HTTP_CACHE = _engine_mod.HttpCache()
        _engine_mod._resource_gate = _engine_mod.ResourceGate()
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset the process-local metrics store between tests.

    ``get_metrics()`` returns the module-level singleton accumulated by
    crawl_request / the engine. Without a reset, metric assertions become
    order-dependent (e.g. a later test could observe non-zero utilization left
    by an earlier recursive crawl). Resetting in place keeps every test hermetic
    while preserving the object identity that ``engine``/``main`` obtain via ``get_metrics()``.
    """
    from app.metrics import get_metrics
    get_metrics().reset()
    yield
    get_metrics().reset()

# NOTE: there is deliberately no autouse extraction-process-pool reset here.
# Tearing down the ProcessPoolExecutor around every test made the suite slow;
# the reset now lives as a scoped fixture in crawl/test_extraction_bounds.py,
# the only module that needs it (it injects env vars into pool workers).
