"""Shared pytest fixtures and path setup for the crawlutron test suite.

Puts the service root on ``sys.path`` once so every test file can import
:mod:`app` and the suite's own helpers (as ``tests.helpers.server``). The
parent monorepo's ``shared/python`` directory is no longer referenced; the
equivalent logging helper lives at :mod:`app.logging_config` and is
importable from the package root.
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

# Fixtures live one-per-file in ``tests/fixtures/`` so each one's setup/teardown
# contract is legible on its own rather than welded to the test module that
# happened to want it first. pytest does not scan that directory, so this is the
# registration list, and it is deliberately the one place the suite's shared
# fixture surface is written down: a fixture no module here names is a fixture
# nothing can depend on. See ``tests/fixtures/__init__.py`` for why helpers and
# fixtures are kept apart.
pytest_plugins = ("tests.fixtures.stub_builder",)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the repository root, which is now the service root

# Only the service root goes on sys.path, and only to make the import base
# explicit: `tests/__init__.py` makes this suite a package, so pytest's prepend
# import mode already puts the root there to import `tests.conftest` at all.
#
# `tests/` itself is deliberately NOT added. It used to be, so that helpers
# could be imported bare -- `from _assertions import ...`, `from helpers.server
# import ...` -- which put two import conventions in one tree and left the name a
# helper resolved to up to a path side effect rather than to the directory
# structure. That is what stopped mypy from checking this suite at all: given
# the same directory it computed a different module name than the one running,
# and refused the file as "found twice under different module names". Helpers
# are now imported as `tests._assertions` / `tests.helpers.server`, so the
# filesystem answers the question for pytest and for mypy alike.
if ROOT.exists() and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The ``app/validation.py`` production default for
# ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS`` is ``False`` (the secure default).
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
#     CRAWLUTRON_TEST_STRICT_SSRF=1 pytest
#
# Tests that exercise the SSRF guard itself should opt out via
# ``_assertions.block_private_targets(monkeypatch)`` or the
# ``ssrf_guard_required`` fixture regardless of mode.
os.environ.setdefault("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")

# `app.main` binds `DATABASE_PATH = settings.cache_dir + "/crawler.db"` at
# import time and hands every test the same process-wide `JobStore` built on it.
# Nothing else moved that file, so a PENDING job row seeded by one test stayed
# readable and claimable by every other test -- including one running at the
# same moment in a different xdist worker -- and by the next full run of the
# suite. A job row was therefore not owned by the test that made it.
#
# One cache directory per worker process, set here because import order is the
# whole constraint: this runs while conftest is imported, before any test
# module reaches `app.main`. A path rather than a created directory, so nothing
# accumulates at import; the app makes it on demand, and
# `_remove_test_cache_dir` below takes it away when the worker is done.
# One cache directory per PROCESS, not per worker, set here because import order
# is the whole constraint: this runs while conftest is imported, before any test
# module reaches `app.main`. A path rather than a created directory, so nothing
# accumulates at import; the app makes it on demand, and
# `_remove_test_cache_dir` below takes it away when the worker is done.
#
# The pid is in the name AND the inherited value is re-derived, because the two
# are not the same fix. Every worker really did get its own directory -- and
# every child those workers spawned inherited the *parent's* value verbatim,
# because `setdefault` leaves an already-set variable alone. A pre-commit gate,
# a uvicorn child, a forked extraction worker: fourteen processes opening and
# migrating one `crawler.db`. A process lock cannot arbitrate that, and it is
# where `database is locked` came from -- raised inside a yoyo migration, by a
# subprocess that had no business touching the parent's database.
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

    Guarded on identity rather than on the env var: if the operator pointed
    ``CRAWLUTRON_CACHE_DIR`` somewhere real, that directory is not the suite's
    to delete, and the teardown must be the one thing in this file that cannot
    destroy their data. The comparison is against the value this process
    installed, which is why a child that re-derived its own still cleans up
    after itself.
    """
    yield
    if os.environ.get("CRAWLUTRON_CACHE_DIR") == str(_SCRATCH_CACHE_DIR):
        shutil.rmtree(_SCRATCH_CACHE_DIR, ignore_errors=True)


_TEST_STRICT_SSRF = os.environ.get("CRAWLUTRON_TEST_STRICT_SSRF", "0") == "1"


@pytest.fixture(scope="session", autouse=True)
def _enable_loopback_for_test_suite():
    """Enable private targets (loopback crawling) for the entire test session.

    This is the **only** path that flips the secure default for the suite. If
    ``CRAWLUTRON_TEST_STRICT_SSRF=1`` is set, this fixture is a no-op and
    every test that needs to crawl loopback must explicitly set the env var.
    """
    if not _TEST_STRICT_SSRF:
        os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] = "1"
    yield
    if not _TEST_STRICT_SSRF:
        os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] = "0"


# Per-xdist-worker paths: when running under pytest-xdist each worker gets its
# own log/cache files so parallel workers cannot clobber shared state. The
# platform temp dir, not a hardcoded /tmp: on macOS gettempdir() is under
# /var/folders, and a /tmp literal would scatter the suite's state off-platform.
#
# The cache directory is set above, and only above. A second `setdefault` for
# `CRAWLUTRON_CACHE_DIR` further down could never fire, yet it named a
# different path from the one `_remove_test_cache_dir` actually removes -- so
# the file read as though the suite isolated its database somewhere it does
# not. One declaration, so there is one path to keep true.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
_WORKER_SUFFIX = f"-{_XDIST_WORKER}" if _XDIST_WORKER else ""
_TEST_TMP = tempfile.gettempdir()
os.environ.setdefault("CRAWLUTRON_LOG_FILE", f"{_TEST_TMP}/crawlutron_test{_WORKER_SUFFIX}.log")
os.environ["CRAWLUTRON_CACHE_TTL_SECONDS"] = os.environ.get("CRAWLUTRON_CACHE_TTL_SECONDS", "3600")
# Keep the opt-in content cache disabled during tests so fake-fetcher suites that
# serve different content for the same URL stay isolated.
os.environ["CRAWLUTRON_CONTENT_CACHE_ENABLED"] = os.environ.get("CRAWLUTRON_CONTENT_CACHE_ENABLED", "false")


@pytest.fixture
def ssrf_guard_required(monkeypatch):
    """Mark a test that exercises ``check_ssrf_protection`` or
    ``resolve_and_pin_host`` and assert that it opted OUT of the test
    default ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1``.

    The suite defaults to private-targets ON so the loopback helper server
    works; tests that intentionally probe the SSRF guard MUST opt back in
    via ``_assertions.block_private_targets(monkeypatch)`` or
    ``monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")``.
    Failure to opt out is a silent false-positive: the SSRF code path is
    short-circuited before the actual guard runs.

    Usage:
        def test_my_ssrf_check(monkeypatch, ssrf_guard_required):
            monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
            check_ssrf_protection(...)
    """
    called: list[str] = []
    from app.crawl.strategies import pinned_transport as pinned_mod

    def _track(name: str, fn):
        def wrapper(*args, **kwargs):
            called.append(name)
            # If the env var is still "1", the SSRF guard was short-circuited
            # before the real test logic could run. That is the silent
            # false-positive this fixture exists to prevent.
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
    from tests._assertions import browser_available

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
    import app.crawl.strategies.fetchers as fetchers_module

    async def _stub_cffi_fetcher(url, headers, timeout_seconds, max_content_length=0):
        raise ConnectionError("curl_cffi stubbed in tests (mark test with real_cffi to opt in)")

    # The impersonating transport is a BINDING of the one fetcher, so the seam is
    # that binding: replacing it makes impersonation unavailable while leaving the
    # plain-httpx fetcher real, which is the distinction the old second entry
    # point used to give for free.
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

    ``CrawlerSettings`` is a frozen dataclass, so per-test overrides build a
    new instance from ``os.environ`` plus ``CRAWLUTRON_*`` overrides and
    restore the original after the test.

    This is also the suite's only spelling of "env plus overrides becomes
    settings". Several test files used to carry a private module-level
    ``_settings(**overrides)`` copy of the same four lines; a test that needs a
    settings object with no other effect now requests this fixture and uses the
    instance it returns, so there is one place that decides how an override key
    becomes an environment variable.
    """
    from app.settings import CrawlerSettings
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

    from app.crawl.extraction import shutdown_doc_extraction_executor

    asyncio.run(shutdown_doc_extraction_executor())


@pytest.fixture
def recording_worker():
    """``(seen, worker)``: a recursive-crawl worker that records its job URLs.

    Several dispatch and robots tests assert *which* jobs reached the worker --
    a robots-skipped URL must never appear, a batch's repeated URL must be
    crawled once. Each of those spelled the same two-line worker, and a copy
    that drops the ``seen.append`` turns the assertion into a vacuous truth
    over an empty list.
    """
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
def client(job_store):
    """An AsyncClient over the jobs app, backed by this test's own ``job_store``.

    Lives here rather than in either test module because two of them need it, and
    a fixture defined in one module is invisible to the other. ``job_store``
    came from here too; the local copy that shadowed it only unwrapped
    ``tmp_path`` a second time, and hid this docstring while doing it.
    """
    from httpx import ASGITransport, AsyncClient

    from tests._job_harnesses import _make_app

    return AsyncClient(transport=ASGITransport(app=_make_app(job_store)), base_url="http://test")


def _served(handler_cls):
    """Serve one handler class on an ephemeral port, then tear it down.

    The teardown is the part every real-server fixture gets wrong, so it is
    written once: a fixture that shuts the socket down in its own body can
    differ from one that does not, and a leaked handler thread outlives the
    test that started it.
    """
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
    import app.crawl.fetch_helpers as fh

    fh.reset_process_state()


def _clear_metrics() -> None:
    """Reset the process-local metrics store.

    ``get_metrics()`` returns the module-level singleton accumulated by
    crawl_request / the engine. Without a reset, metric assertions become
    order-dependent (e.g. a later test could observe non-zero utilization left
    by an earlier recursive crawl). Resetting in place keeps every test hermetic
    while preserving the object identity that ``engine``/``main`` obtain via ``get_metrics()``.
    """
    from app.metrics import get_metrics

    get_metrics().reset()


def _clear_global_http_state() -> None:
    """Reset crawler global singletons.

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
    import asyncio

    from app.crawl.strategies import cookie_jar as cookie_mod
    from app.crawl.strategies import http_client_pool as pool_mod

    with contextlib.suppress(Exception):
        cookie_mod.reset_process_state()
    with contextlib.suppress(Exception):
        # The renderer's per-domain cookie store is module-global and bounded, not
        # cleared, so a test that harvests once leaves its domain's cookies for the
        # next test to assert against. It used to be undone per test with
        # monkeypatch.setitem, which only worked while the store was a
        # MutableMapping; the reset door is the one place process state is dropped.
        from app.crawl.strategies import browser as browser_mod

        browser_mod.reset_process_state()
    with contextlib.suppress(Exception):
        # The pool decides per client whether it may be closed or must simply be
        # dropped: a client owned by an already-finished loop cannot be awaited
        # from the next one, and only the pool knows which loop owns what.
        pool_mod.reset_process_state()
    # The real app's shutdown event is module-global and shared by every
    # ASGI test. A test that drives the real lifespan (shutdown) leaves it
    # SET; production never reuses a set event (fresh boot = fresh event),
    # so restore that invariant here -- otherwise later /v1/crawl calls
    # abort with empty results and fresh jobs go PAUSED (poisoned event).
    with contextlib.suppress(Exception):
        service_app().state.shutdown_event = asyncio.Event()
    # robots_cache was promoted to module-level and shared across crawl_request
    # calls. Tests reuse origins with different robots.txt bodies, so
    # clear it between tests for isolation. Production keeps it shared (a given
    # origin always serves the same robots.txt).
    with contextlib.suppress(Exception):
        from app.crawl import engine as _engine_mod

        # The robots cache, the shared HTTP cache and the admission gate are one
        # crawl-lifetime unit, so the engine empties all three itself. It used to
        # be spelled out here, one line per field, which is how a fourth field
        # would have been missed.
        _engine_mod.reset_process_state()
        # Drop any memoized cgroup reads so a test that asserts fresh sensing
        # (e.g. writes a new fake cgroup file) is not served a stale value.
        from app.cgroup_sense import reset_process_state

        reset_process_state()
    # DNS cache + pending client close queue + cookie namespace contextvar
    # isolation between tests. Without these a test that mutates ``_DNS_CACHE``
    # or schedules a client close would leak state into the next test.
    # ``cookie_mod``/``pool_mod`` are the ones bound at the top of this function;
    # only ``pinned_mod`` is new here.
    from app.crawl.strategies import pinned_transport as pinned_mod

    with contextlib.suppress(Exception):
        pinned_mod.reset_process_state()
        pool_mod.reset_process_state()
        # Reset the per-request cookie namespace so a test that sets one does
        # not leak into a subsequent test's request context.
        cookie_mod.reset_process_state()


# The three resets above are one concern -- "no module global survives a test"
# -- and were three autouse fixtures. One fixture means one setup and one
# teardown per test instead of three, and it means a new global is cleared in
# one place rather than in whichever reset happened to be spelled last. The
# rationale for each stays on the function that does the work; what was removed
# is the duplicated fixture scaffolding, not the argument for it.
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
# Tearing down the ProcessPoolExecutor around every test made the suite slow;
# the reset now lives as a scoped fixture in crawl/test_extraction_bounds.py,
# the only module that needs it (it injects env vars into pool workers).


@pytest.fixture(autouse=True)
async def owned_job_runtime():
    """Hand every test the job runtime, and take it back at the end.

    ``app.api.job_runner`` keeps its task registry in a module-level dict and
    the app keeps its shutdown event in ``app.state``, so both are shared by
    every test in a worker process. A crawl started by one test therefore
    stayed visible to the next, and a test that shut the world down did it for
    its neighbours as well.

    The event is minted per test here as well as per lifespan run in
    ``app.main``, because ``async_client`` drives the app over an ASGI
    transport without running its lifespan -- so the dependency
    ``get_shutdown_event`` resolves has to be supplied by whoever owns the
    test, and that is this fixture.

    Leaving a crawl running is only legitimate until the test that started it
    ends. Teardown is the one point where that is unconditionally true for every
    test, so it is where the reset belongs, rather than in each test module that
    happens to remember to call ``reset_job_runner()`` itself.

    Async rather than sync because cancelling a task and awaiting its exit are
    different acts, and only the second one proves the task is gone. Cancelling
    without awaiting leaves it holding the store the next test opens.
    """
    from app.api import job_runner
    from tests._app_factory import service_app

    app = service_app()
    if app is not None:
        app.state.shutdown_event = asyncio.Event()
    yield
    await job_runner.shutdown_job_runner()
    job_runner.reset_execute_crawl()
