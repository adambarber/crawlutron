"""Shared pytest fixtures and path setup, putting the service root on ``sys.path``
once so ``app`` and ``tests.helpers`` resolve; the monorepo's ``shared/python`` is
no longer referenced -- the logging helper lives at :mod:`app.runtime.logging_config`.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from tests._app_factory import service_app

# Fixtures live one-per-file in ``tests/fixtures/`` so each one's setup/teardown contract is legible on its
# own rather than welded to the test module that happened to want it first.
pytest_plugins = ("tests.fixtures.stub_builder", "tests.fixtures.factory")

# The real asyncio.sleep, kept so the zero-second patch below can still yield.
_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep


async def _fast_asyncio_sleep(delay, *args, **kwargs):
    """A zero-second async sleep: it waits for nothing, but it still yields.

    A never-suspending patch turns yield-point sleeps into hot spins that hang
    the suite; yielding once through the real sleep keeps that intact."""
    del delay, args, kwargs
    await _ORIGINAL_ASYNCIO_SLEEP(0)


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the repository root, which is now the service root

# Only the service root goes on sys.path, and only to make the import base explicit: `tests/__init__.py` makes
# this suite a package, so pytest's prepend import mode already puts the root there to import
if ROOT.exists() and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The ``app/safety/validation.py`` production default for ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS`` is ``False``
# (the secure default).
os.environ.setdefault("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")

# `app.main` binds `DATABASE_PATH = settings.cache_dir + "/crawler.db"` at import time and hands every test
# the same process-wide `JobStore` built on it.
_SCRATCH_CACHE_DIR = (
    Path(tempfile.gettempdir())
    / f"crawlutron-tests-{os.environ.get('PYTEST_XDIST_WORKER', 'master')}-{os.getpid()}"
)
_INHERITED_CACHE_DIR = os.environ.get("CRAWLUTRON_CACHE_DIR", "")
_INHERITED_IS_SUITE_SCRATCH = (
    _INHERITED_CACHE_DIR.startswith(f"{Path(tempfile.gettempdir())}{os.sep}")
    and "crawlutron-tests-" in _INHERITED_CACHE_DIR
)
if _INHERITED_IS_SUITE_SCRATCH:
    # A parent process in this suite exported its scratch dir. Derive our own.
    os.environ["CRAWLUTRON_CACHE_DIR"] = str(_SCRATCH_CACHE_DIR)
else:
    os.environ.setdefault("CRAWLUTRON_CACHE_DIR", str(_SCRATCH_CACHE_DIR))


@pytest.fixture(scope="session", autouse=True)
def _remove_test_cache_dir():
    """Delete this worker's scratch cache directory once the session is over.

    Guarded on identity: if the operator pointed CRAWLUTRON_CACHE_DIR
    somewhere real, that directory is not the suite's to delete."""
    yield
    if os.environ.get("CRAWLUTRON_CACHE_DIR") == str(_SCRATCH_CACHE_DIR):
        shutil.rmtree(_SCRATCH_CACHE_DIR, ignore_errors=True)


_TEST_STRICT_SSRF = os.environ.get("CRAWLUTRON_TEST_STRICT_SSRF", "0") == "1"


@pytest.fixture(scope="session", autouse=True)
def _enable_loopback_for_test_suite():
    """Enable private targets (loopback crawling) for the test session.

    The only path that flips the secure default; strict-SSRF runs leave it on
    and each loopback test opts in per test."""
    if not _TEST_STRICT_SSRF:
        os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] = "1"
    yield
    if not _TEST_STRICT_SSRF:
        os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] = "0"


# Per-xdist-worker paths: when running under pytest-xdist each worker gets its own log/cache files so parallel
# workers cannot clobber shared state.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
_WORKER_SUFFIX = f"-{_XDIST_WORKER}" if _XDIST_WORKER else ""
_TEST_TMP = tempfile.gettempdir()
os.environ.setdefault("CRAWLUTRON_LOG_FILE", f"{_TEST_TMP}/crawlutron_test{_WORKER_SUFFIX}.log")
os.environ["CRAWLUTRON_CACHE_TTL_SECONDS"] = os.environ.get("CRAWLUTRON_CACHE_TTL_SECONDS", "3600")
# Keep the opt-in content cache disabled during tests so fake-fetcher suites that serve different content for
# the same URL stay isolated.
os.environ["CRAWLUTRON_CONTENT_CACHE_ENABLED"] = os.environ.get("CRAWLUTRON_CONTENT_CACHE_ENABLED", "false")


@pytest.fixture
def ssrf_guard_required(monkeypatch):
    """Mark a test that exercises an SSRF guard and assert it opted out of
    the test default CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1; otherwise the guard
    short-circuits before the real path runs."""
    called: list[str] = []
    from app.transport import pinned_transport as pinned_mod

    def _track(name: str, fn):
        def wrapper(*args, **kwargs):
            called.append(name)
            # That is the silent false-positive this fixture exists to prevent.
            if os.environ.get("CRAWLUTRON_ALLOW_PRIVATE_TARGETS") != "0":
                pytest.fail(
                    f"{name} was called but CRAWLUTRON_ALLOW_PRIVATE_TARGETS "
                    f"is still enabled (value={os.environ.get('CRAWLUTRON_ALLOW_PRIVATE_TARGETS')!r}). "
                    f"This test exercises the SSRF guard and must opt out via "
                    f"_assertions.block_private_targets(monkeypatch) or "
                    f"monkeypatch.setenv('CRAWLUTRON_ALLOW_PRIVATE_TARGETS', '0') "
                    f"BEFORE invoking the SSRF helper."
                )
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        pinned_mod,
        "check_ssrf_protection",
        _track("check_ssrf_protection", pinned_mod.check_ssrf_protection),
    )
    monkeypatch.setattr(
        pinned_mod, "resolve_and_pin_host", _track("resolve_and_pin_host", pinned_mod.resolve_and_pin_host)
    )
    yield
    # Sanity check in setup phase (not teardown) so the failure surfaces as a clean FAILED test, not an ERROR
    # from the fixture finalizer.
    _ = called  # silence unused warning if a future check is added


def pytest_collection_modifyitems(config, items):
    """Auto-skip browser-marked tests when no real browser is installed."""
    from tests._assertions import browser_available

    if browser_available():
        return
    skip_no_browser = pytest.mark.skip(reason="browser not available in this environment")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip_no_browser)


@pytest.fixture(autouse=True)
def fast_sleeps(request):
    """Zero ``time.sleep`` and ``asyncio.sleep`` unless the test opts out.

    Timing-true tests carry the ``real_sleeps`` marker instead."""
    if request.node.get_closest_marker("real_sleeps"):
        yield
        return
    with patch("time.sleep", lambda *a, **k: None), patch("asyncio.sleep", _fast_asyncio_sleep):
        yield


@pytest.fixture(autouse=True)
def stub_cffi_fetcher(request, monkeypatch):
    """Replace the real curl_cffi TLS-impersonation fetcher in tests.

    Stream-mode curl_cffi busy-spins the GIL under fast_sleeps; real cffi
    runs only in subprocess-isolated and ``real_cffi``-marked tests."""
    if request.node.get_closest_marker("real_cffi"):
        return
    import app.transport.fetchers as fetchers_module

    async def _stub_cffi_fetcher(url, headers, timeout_seconds, max_content_length=0):
        raise ConnectionError("curl_cffi stubbed in tests (mark test with real_cffi to opt in)")

    monkeypatch.setattr(fetchers_module, "impersonating_fetcher", lambda: _stub_cffi_fetcher)


@pytest.fixture(scope="session")
def test_server():
    """Start a real in-process HTTP server once per test session."""
    from tests.helpers.server import spawn_server

    server = spawn_server()
    yield server
    server.stop()


@pytest.fixture
def base_url(test_server):
    """The base URL of the session-scoped test server."""
    return test_server.base_url


@pytest.fixture
async def async_client():
    """Async HTTP client using ASGI transport for FastAPI app."""
    async with httpx.ASGITransport(app=service_app()) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def override_settings():
    """Point ``app.state.crawler_settings`` at a fresh instance for a test.

    The suite's only spelling of "env plus overrides becomes settings";
    test files used to carry private module-level copies of the same lines."""
    from app.config.settings import CrawlerSettings
    from tests._app_factory import service_app

    app = service_app()

    original = app.state.crawler_settings

    def _apply(**overrides) -> CrawlerSettings:
        merged = dict(os.environ)
        for key, value in overrides.items():
            merged[f"CRAWLUTRON_{key.upper()}"] = str(value)
        settings = CrawlerSettings.from_mapping(merged)
        app.state.crawler_settings = settings
        return settings

    yield _apply
    app.state.crawler_settings = original


@pytest.fixture
def fake_provider_http(monkeypatch):
    """Patch ``app.search.providers.provider_http`` with a scripted response.

    Returns a callable that installs ``(status, body)`` and optionally records
    the kwargs of every provider HTTP call into ``capture``.
    """

    def _patch(status: int = 200, body: str = "{}", capture: list[dict[str, object]] | None = None):
        async def _fake(method, url, **kwargs):
            if capture is not None:
                capture.append({"method": method, "url": url, **kwargs})
            return status, body

        monkeypatch.setattr("app.search.providers.provider_http", _fake)
        return _fake

    return _patch


@pytest.fixture(autouse=True, scope="session")
def _cleanup_extraction_executors():
    """Shutdown the document extraction process pool at end of test session."""
    yield
    import asyncio

    from app.extraction.content import shutdown_doc_extraction_executor

    asyncio.run(shutdown_doc_extraction_executor())


@pytest.fixture(autouse=True)
def _document_pool_per_test(monkeypatch):
    """Each test gets its own document pool.

    The pool is a process global whose teardown is terminal: a test that ran
    the lifespan closed it for every later test in the same worker.
    """
    import app.extraction.content as ext_mod
    import app.extraction.documents as docex

    pool = docex.build_doc_pool()
    monkeypatch.setattr(docex, "DOC_POOL", pool)
    monkeypatch.setattr(ext_mod, "DOC_POOL", pool)
    yield pool
    # The pool this test used, and any worker it spawned, dies with the test.
    pool.teardown()


@pytest.fixture
def recording_worker():
    """``(seen, worker)``: a recursive-crawl worker that records its job URLs.

    Several dispatch and robots tests assert *which* jobs reached the worker;
    a copy that drops the ``seen.append`` turns that into a vacuous truth."""
    from app.models.normalized_url import NormalizedUrl
    from tests._result_builders import make_engine_result

    seen: list[NormalizedUrl] = []

    async def worker(job):
        seen.append(job.normalized_url)
        return make_engine_result(job.normalized_url)

    return seen, worker


@pytest.fixture
def base_policy():
    """Fresh minimal crawl policy; tests may mutate their copy freely."""
    from tests._policy_factories import make_base_policy

    return make_base_policy()


@pytest.fixture
def job_store(tmp_path):
    """JobStore backed by a per-test SQLite db under ``tmp_path``."""
    from tests._job_harnesses import make_job_store

    return make_job_store(str(tmp_path))


@pytest.fixture
def watch_store(tmp_path):
    """WatchStore backed by a per-test SQLite db under ``tmp_path``."""
    from app.storage.watch_store import WatchStore

    return WatchStore(tmp_path / "watches.db")


@pytest.fixture
def client(job_store):
    """An AsyncClient over the jobs app, backed by this test's own ``job_store``.

    Lives here rather than in either test module because both need it; the
    local copy that shadowed the fixture only rewrapped tmp_path."""
    from httpx import ASGITransport, AsyncClient

    from tests._job_harnesses import _make_app

    return AsyncClient(transport=ASGITransport(app=_make_app(job_store)), base_url="http://test")


def _served(handler_cls):
    """Serve one handler class on an ephemeral port, then tear it down.

    The teardown is written once: a leaked handler thread outlives the test
    that started it."""
    from tests.helpers.server import spawn_threaded_server

    server = spawn_threaded_server(handler_cls)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def counting_server():
    """Real HTTP server serving one cacheable page; counts hits on ``server.hits``."""
    from tests.helpers.server import CountingHandler

    yield from _served(CountingHandler)


@pytest.fixture
def etag_server():
    """Real HTTP server honoring If-None-Match; counts hits on ``server.hits``.

    Mutate ``helpers.server.EtagHandler.etag`` / ``.body`` to simulate an
    upstream content change.
    """
    from tests.helpers.server import EtagHandler

    yield from _served(EtagHandler)


def _clear_sitemap_cache() -> None:
    """Clear the module-level sitemap TTL cache.

    Tests inject fake fetchers serving different content for the same
    sitemap URLs; without this the shared cache leaks across tests.
    """
    import app.orchestration.fetch_helpers as fh

    fh.reset_process_state()


def _clear_metrics() -> None:
    """Reset the process-local metrics store.

    Without it, metric assertions become order-dependent: a later test could
    observe utilization left by an earlier crawl."""
    from app.runtime.metrics import get_metrics

    get_metrics().reset()


def _clear_global_http_state() -> None:
    """Reset crawler global singletons.

    Dead-loop-bound pooled clients leak sockets, cookies and cache state across
    tests; the close is loop-aware (drop for GC) to avoid running a second loop."""
    import asyncio

    from app.transport import cookie_jar as cookie_mod
    from app.transport import http_client_pool as pool_mod

    with contextlib.suppress(Exception):
        cookie_mod.reset_process_state()
    with contextlib.suppress(Exception):
        # The renderer's per-domain cookie store is module-global and bounded, not cleared, so a test that
        # harvests once leaves its domain's cookies for the next test to assert against.
        from app.transport import browser as browser_mod

        browser_mod.reset_process_state()
    with contextlib.suppress(Exception):
        # The pool decides per client whether it may be closed or must simply be dropped: a client owned by an
        # already-finished loop cannot be awaited from the next one, and only the pool knows which loop owns
        pool_mod.reset_process_state()
    # The real app's shutdown event is module-global and shared by every ASGI test.
    with contextlib.suppress(Exception):
        service_app().state.shutdown_event = asyncio.Event()
    # Tests reuse origins with different robots.txt bodies, so clear it between tests for isolation.
    # Production keeps it shared (a given origin always serves the same robots.txt).
    with contextlib.suppress(Exception):
        from app.orchestration import engine as _engine_mod

        # The robots cache, the shared HTTP cache and the admission gate are one crawl-lifetime unit, so the
        # engine empties all three itself.
        _engine_mod.reset_process_state()
        # Drop any memoized cgroup reads so a test that asserts fresh sensing (e.g. writes a new fake cgroup
        # file) is not served a stale value.
        from app.runtime.cgroup_sense import reset_process_state

        reset_process_state()
    # DNS cache + pending client close queue + cookie namespace contextvar isolation between tests
    from app.transport import pinned_transport as pinned_mod

    with contextlib.suppress(Exception):
        pinned_mod.reset_process_state()
        pool_mod.reset_process_state()
        # Reset the per-request cookie namespace so a test that sets one does not leak into a subsequent
        # test's request context.
        cookie_mod.reset_process_state()


_MODULE_GLOBAL_RESETS = (_clear_sitemap_cache, _clear_metrics, _clear_global_http_state)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """Clear every process-global the production code shares, around each test."""
    for reset in _MODULE_GLOBAL_RESETS:
        reset()
    yield
    for reset in _MODULE_GLOBAL_RESETS:
        reset()


# NOTE: there is deliberately no autouse extraction-process-pool reset here.


@pytest.fixture(autouse=True)
async def owned_job_runtime():
    """Hand every test the job runtime, and take it back at the end.

    The task registry and shutdown event are module-global: a crawl from one
    test stayed visible to the next. Teardown is where the reset belongs."""
    from app.api import job_runner
    from tests._app_factory import service_app

    app = service_app()
    if app is not None:
        app.state.shutdown_event = asyncio.Event()
    yield
    await job_runner.shutdown_job_runner()
    job_runner.reset_execute_crawl()
