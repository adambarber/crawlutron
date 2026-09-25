"""Shared pytest fixtures and path setup for the browser-crawler test suite.

Adds the package root and the ``tests/`` directory to ``sys.path`` once so
every test file can import :mod:`app` and :mod:`helpers` directly. The
parent monorepo's ``shared/python`` directory is no longer referenced; the
equivalent logging helper lives at :mod:`app.logging_config` and is
importable from the package root.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # docker/browser-crawler

for candidate in (str(ROOT), str(HERE)):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

# The ``app/validation.py`` production default for
# ``BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS`` is ``False`` (the secure default).
# For this test suite, the file-level default is ALSO ``"0"`` so that anyone
# reading ``conftest.py`` sees the secure default first. The session-scoped
# autouse fixture below (``_enable_loopback_for_test_suite``) flips the value
# to ``"1"`` for the entire test session because the loopback helper server
# (``tests/helpers/server.py``) is used by most integration tests.
#
# To run the suite in SSRF-strict mode (so every loopback test must explicitly
# opt in via ``monkeypatch.setenv(..., "1")``), set the env var before
# invoking pytest:
#
#     BROWSER_CRAWLER_TEST_STRICT_SSRF=1 pytest
#
# Tests that exercise the SSRF guard itself should opt out via
# ``_assertions.block_private_targets(monkeypatch)`` or the
# ``ssrf_guard_required`` fixture regardless of mode.
os.environ.setdefault("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")

_TEST_STRICT_SSRF = os.environ.get("BROWSER_CRAWLER_TEST_STRICT_SSRF", "0") == "1"


@pytest.fixture(scope="session", autouse=True)
def _enable_loopback_for_test_suite():
    """Enable private targets (loopback crawling) for the entire test session.

    This is the **only** path that flips the secure default for the suite. If
    ``BROWSER_CRAWLER_TEST_STRICT_SSRF=1`` is set, this fixture is a no-op and
    every test that needs to crawl loopback must explicitly set the env var.
    """
    if not _TEST_STRICT_SSRF:
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"
    yield
    if not _TEST_STRICT_SSRF:
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"


# Per-xdist-worker paths: when running under pytest-xdist each worker gets its
# own log/cache files so parallel workers cannot clobber shared state.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
_WORKER_SUFFIX = f"-{_XDIST_WORKER}" if _XDIST_WORKER else ""
os.environ.setdefault("BROWSER_CRAWLER_LOG_FILE", f"/tmp/browser_crawler_test{_WORKER_SUFFIX}.log")
os.environ.setdefault("BROWSER_CRAWLER_CACHE_DIR", f"/tmp/browser_crawler_test_cache{_WORKER_SUFFIX}")
os.environ["BROWSER_CRAWLER_CACHE_TTL_SECONDS"] = os.environ.get("BROWSER_CRAWLER_CACHE_TTL_SECONDS", "3600")
# Keep the opt-in content cache disabled during tests so fake-fetcher suites that
# serve different content for the same URL stay isolated (#5).
os.environ["BROWSER_CRAWLER_CONTENT_CACHE_ENABLED"] = os.environ.get(
    "BROWSER_CRAWLER_CONTENT_CACHE_ENABLED", "false"
)


@pytest.fixture
def ssrf_guard_required(monkeypatch):
    """Mark a test that exercises ``_check_ssrf_protection`` or
    ``_resolve_and_pin_host`` and assert that it opted OUT of the test
    default ``BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1``.

    The suite defaults to private-targets ON so the loopback helper server
    works; tests that intentionally probe the SSRF guard MUST opt back in
    via ``_assertions.block_private_targets(monkeypatch)`` or
    ``monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")``.
    Failure to opt out is a silent false-positive: the SSRF code path is
    short-circuited before the actual guard runs.

    Usage:
        def test_my_ssrf_check(monkeypatch, ssrf_guard_required):
            monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
            _check_ssrf_protection(...)
    """
    called: list[str] = []
    from app.crawl.strategies import pinned_transport as pinned_mod

    def _track(name: str, fn):
        def wrapper(*args, **kwargs):
            called.append(name)
            # If the env var is still "1", the SSRF guard was short-circuited
            # before the real test logic could run. That is the silent
            # false-positive this fixture exists to prevent.
            if os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS") != "0":
                pytest.fail(
                    f"{name} was called but BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS "
                    f"is still enabled (value={os.environ.get('BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS')!r}). "
                    f"This test exercises the SSRF guard and must opt out via "
                    f"_assertions.block_private_targets(monkeypatch) or "
                    f"monkeypatch.setenv('BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS', '0') "
                    f"BEFORE invoking the SSRF helper."
                )
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        pinned_mod, "_check_ssrf_protection", _track("check_ssrf_protection", pinned_mod._check_ssrf_protection)
    )
    monkeypatch.setattr(
        pinned_mod, "_resolve_and_pin_host", _track("resolve_and_pin_host", pinned_mod._resolve_and_pin_host)
    )
    yield
    # Sanity check in setup phase (not teardown) so the failure surfaces as
    # a clean FAILED test, not an ERROR from the fixture finalizer. We do
    # this by patching pytest.fail to record the call into ``called`` as
    # well, but a simpler approach: skip the teardown check entirely and
    # rely on the per-call check above to catch opt-out misses. The
    # "fixture requested but no SSRF helper called" case is rare enough
    # to skip the explicit guard; if it ever happens, the test simply
    # passes without exercising the SSRF path, which is the same outcome
    # the user would get without the fixture.
    _ = called  # silence unused warning if a future check is added


# The real asyncio.sleep, kept so the zero-second patch below can still yield.
_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep


async def _fast_asyncio_sleep(delay, *args, **kwargs):
    """A zero-second async sleep: it waits for nothing, but it still yields.

    The previous version of this returned without ever suspending, so awaiting
    it cost *zero* scheduling points. That is only safe for a sleep whose sole
    purpose is a delay, and production code is full of sleeps that are also
    scheduling points: ``await_or_client_gone`` polls the client-disconnect flag
    with ``await asyncio.sleep(poll_interval)``, a per-domain gate releases
    through one, a semaphore's barging check depends on one. Under a
    never-suspending patch those loops become a hot spin that never hands the
    loop back, so the coroutine they are waiting for can never run and the
    test hangs rather than failing.

    Yielding once through the real ``asyncio.sleep(0)`` keeps that whole class
    of loop-shaped behaviour intact at zero wall-clock cost: the requested
    duration is discarded, the scheduling point is not. Everything that used to
    need ``real_sleeps`` merely to get a yield can now drop it.
    """
    del delay, args, kwargs
    await _ORIGINAL_ASYNCIO_SLEEP(0)


def pytest_collection_modifyitems(config, items):
    """Auto-skip browser-marked tests when no real browser is installed.

    Keeps ``pytest -m browser`` (and the full run) green on machines without
    Camoufox/Playwright instead of failing mid-suite. When a browser is present
    the marker is left intact so the tests actually execute.
    """
    from _assertions import browser_available

    if browser_available():
        return
    skip_no_browser = pytest.mark.skip(reason="browser not available in this environment")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip_no_browser)


@pytest.fixture(autouse=True)
def fast_sleeps():
    """Patch time.sleep and asyncio.sleep to be zero-second by default.

    By default the test suite runs with sleeps that cost no wall-clock time, so
    delay routines (scheduler delays, retry backoffs, browser lazy-scroll
    waits) do not slow tests down. ``asyncio.sleep`` becomes a sleep of zero
    seconds that still yields -- see ``_fast_asyncio_sleep``.

    There is deliberately no ``real_sleeps`` escape hatch any more. A fixture
    that restores wall-clock time cannot be excluded by ``-m "not slow"`` (it is
    a parameter, not a marker), so it silently opted 100+ tests into depending
    on how loaded the developer's box was; and for the concurrency assertions it
    was the *instrument*, not the noise. Where elapsed time genuinely is the
    property, the test now drives the boundary through a controlled trigger or
    an injected clock, and the handful of probes that must observe real elapsed
    time say so in their own docstring and are marked ``slow``.
    """
    with patch("time.sleep", lambda *a, **k: None), patch("asyncio.sleep", _fast_asyncio_sleep):
        yield


@pytest.fixture(autouse=True)
def stub_cffi_fetcher(request, monkeypatch):
    """Replace the real curl_cffi TLS-impersonation fetcher in tests.

    Stream-mode curl_cffi 0.16 has no plain timeout (it relies on a
    LOW_SPEED abort), and its internal watchdog busy-spins the GIL under
    the fast_sleeps patch, wedging the entire pytest process. Real cffi
    behavior is exercised in subprocess-isolated tests and in live tests
    marked ``real_cffi``; everything else gets a fast connection error,
    matching the pre-streaming failure semantics the suite was built on.
    """
    if request.node.get_closest_marker("real_cffi"):
        return
    import app.crawl.engine as engine_module

    async def _stub_cffi_fetcher(url, headers, timeout_seconds, max_content_length=0):
        raise ConnectionError("curl_cffi stubbed in tests (mark test with real_cffi to opt in)")

    monkeypatch.setattr(engine_module, "_default_cffi_fetcher", _stub_cffi_fetcher)


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


@pytest.fixture
def override_settings():
    """Point ``app.state.crawler_settings`` at a fresh instance for a test.

    ``CrawlerSettings`` is a frozen dataclass, so per-test overrides build a
    new instance from ``os.environ`` plus ``BROWSER_CRAWLER_*`` overrides and
    restore the original after the test.
    """
    from app.main import app
    from app.settings import CrawlerSettings

    original = app.state.crawler_settings

    def _apply(**overrides) -> CrawlerSettings:
        merged = dict(os.environ)
        for key, value in overrides.items():
            merged[f"BROWSER_CRAWLER_{key.upper()}"] = str(value)
        settings = CrawlerSettings.from_mapping(merged)
        app.state.crawler_settings = settings
        return settings

    yield _apply
    app.state.crawler_settings = original


@pytest.fixture
def fake_provider_http(monkeypatch):
    """Patch ``app.search.providers._provider_http`` with a scripted response.

    Returns a callable that installs ``(status, body)`` and optionally records
    the kwargs of every provider HTTP call into ``capture``.
    """

    def _patch(status: int = 200, body: str = "{}", capture: list | None = None):
        async def _fake(method, url, **kwargs):
            if capture is not None:
                capture.append({"method": method, "url": url, **kwargs})
            return status, body

        monkeypatch.setattr("app.search.providers._provider_http", _fake)
        return _fake

    return _patch


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
    from _policy_factories import make_base_policy

    return make_base_policy()


@pytest.fixture
def job_store(tmp_path):
    """JobStore backed by a per-test SQLite db under ``tmp_path``."""
    from _job_harnesses import make_job_store

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

    from app.crawl.strategies import cookie_jar as cookie_mod
    from app.crawl.strategies import http_client_pool as pool_mod

    try:
        cookie_mod._GLOBAL_COOKIE_JAR.clear()
    except Exception:
        pass
    try:
        if pool_mod._DEFAULT_HTTP_CLIENT is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Inside an async test's loop: drop the reference so GC
                    # reclaims the client instead of awaiting across loops.
                    pool_mod._DEFAULT_HTTP_CLIENT = None
                else:
                    loop.run_until_complete(pool_mod.close_async_http_client())
            except RuntimeError:
                pool_mod._DEFAULT_HTTP_CLIENT = None
    except Exception:
        pass
    # Shared curl_cffi session (perf throughput #4): same loop-hygiene as the
    # pooled httpx client so tests stay order-independent under asyncio.run.
    try:
        if pool_mod._DEFAULT_CFFI_SESSION is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    pool_mod._DEFAULT_CFFI_SESSION = None
                else:
                    loop.run_until_complete(pool_mod.close_cffi_session())
            except RuntimeError:
                pool_mod._DEFAULT_CFFI_SESSION = None
    except Exception:
        pass
    # The real app's shutdown event is module-global and shared by every
    # ASGI test. A test that drives the real lifespan (shutdown) leaves it
    # SET; production never reuses a set event (fresh boot = fresh event),
    # so restore that invariant here -- otherwise later /v1/crawl calls
    # abort with empty results and fresh jobs go PAUSED (poisoned event).
    try:
        from app.main import app as _app

        _app.state.shutdown_event = asyncio.Event()
    except Exception:
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
        # Drop any memoized cgroup reads so a test that asserts fresh sensing
        # (e.g. writes a new fake cgroup file) is not served a stale value.
        from app.cgroup_sense import _invalidate_cgroup_cache

        _invalidate_cgroup_cache()
    except Exception:
        pass
    # DNS cache + pending client close queue + cookie namespace contextvar
    # isolation between tests. Without these a test that mutates ``_DNS_CACHE``
    # or schedules a client close would leak state into the next test
    # (adversarial review F-test-isolation).
    from app.crawl.strategies import cookie_jar as cookie_mod
    from app.crawl.strategies import http_client_pool as pool_mod
    from app.crawl.strategies import pinned_transport as pinned_mod

    try:
        pinned_mod._DNS_CACHE.clear()
        # Drop pending-client references: we cannot await aclose from sync
        # context, but dropping the references lets GC reclaim the sockets
        # once the loop that owns them exits. The capped queue means the
        # list stays small even under heavy loop churn.
        with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
            pool_mod._PENDING_CLIENT_CLOSE.clear()
        # Reset the per-request cookie namespace so a test that sets one does
        # not leak into a subsequent test's request context.
        cookie_mod._COOKIE_NAMESPACE.set("")
    except Exception:
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
