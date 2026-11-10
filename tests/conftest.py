"""Shared pytest fixtures and path setup, putting the service root on ``sys.path``
once so ``app`` and ``tests.helpers`` resolve; the monorepo's ``shared/python`` is
no longer referenced -- the logging helper lives at :mod:`crawlutron.runtime.logging_config`.
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TextIO

import pytest

from tests._app_factory import service_app

# The real PBKDF2 work factor, captured at import before the autouse clamp
# fixture can patch it: the floor pin reads this, not the live attribute.
_REAL_PBKDF2_ITERATIONS: int | None = None
try:
    from crawlutron.storage import caller_key_store as _caller_key_store

    _REAL_PBKDF2_ITERATIONS = _caller_key_store.PBKDF2_ITERATIONS
except ImportError:  # pragma: no cover
    pass

# Fixtures live one-per-file in ``tests/fixtures/``: one legible contract each, never welded to a first user.
pytest_plugins = (
    "tests.fixtures.stub_builder",
    "tests.fixtures.factory",
    "tests.fixtures.four_worker_selector_pool",
    "tests.fixtures.test_server_fixture",
    "tests.fixtures.base_url",
    "tests.fixtures.async_client",
    "tests.fixtures.override_settings",
    "tests.fixtures.ssrf_guard_required",
    "tests.fixtures.fake_provider_http",
    "tests.fixtures.recording_worker",
    "tests.fixtures.watch_store",
    "tests.fixtures.client",
    "tests.fixtures.real_http_servers",
    "tests.network_guard",
)

# The real asyncio.sleep, kept so the fast dispatcher can still yield through it.
_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep
_REAL_TIME_SLEEP = time.sleep

# The suite's stable sleep dispatcher, installed once and never uninstalled:
# a sibling's captured restore round-trips to THIS binding, never the fast sleep.
_SLEEP_MODE = {"fast": False}


async def _dispatched_asyncio_sleep(delay, *args, **kwargs):
    """Fast mode waits for nothing but still yields once (a never-suspending
    patch turns yield-point sleeps into hot spins); real mode passes through."""
    if _SLEEP_MODE["fast"]:
        del args, kwargs
        await _ORIGINAL_ASYNCIO_SLEEP(0)
        return
    await _ORIGINAL_ASYNCIO_SLEEP(delay)


def _dispatched_time_sleep(delay=None):
    """Fast mode is a no-op; real mode is the real time.sleep."""
    if not _SLEEP_MODE["fast"]:
        _REAL_TIME_SLEEP(delay if delay is not None else 0)


asyncio.sleep = _dispatched_asyncio_sleep
time.sleep = _dispatched_time_sleep


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the repository root, which is now the service root

# Only the service root goes on sys.path, and only to make the import base explicit: `tests/__init__.py` makes
# this suite a package, so pytest's prepend import mode already puts the root there to import
if ROOT.exists() and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The ``crawlutron/safety/validation.py`` default for ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS`` is ``False``
# (the secure default).
os.environ.setdefault("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")

# `crawlutron.main` binds `DATABASE_PATH = settings.cache_dir + "/crawler.db"` at import; every test
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


@pytest.fixture(scope="session", autouse=True)
def _enable_loopback_for_test_suite():
    """Enable private targets (loopback crawling) for the test session."""
    os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] = "1"
    yield
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


def pytest_collection_modifyitems(config, items):
    """Auto-skip browser-marked tests when no real browser is installed."""
    from tests._assertions import browser_available

    if browser_available():
        return
    skip_no_browser = pytest.mark.skip(reason="browser not available in this environment")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip_no_browser)


# 435: zero-run xdist workers abort natively when teardown races the camoufox
# import graph's shutdown; unbuffered streams shrink that race.
try:
    # ``reconfigure`` exists only on TextIOWrapper, not every TextIO; the
    # except arm already tolerates both the attr and value gaps.
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr
    if hasattr(stdout, "reconfigure") and hasattr(stderr, "reconfigure"):
        stdout.reconfigure(line_buffering=False, write_through=True)
        stderr.reconfigure(line_buffering=False, write_through=True)
    os.environ["PYTHONUNBUFFERED"] = "1"
except (AttributeError, ValueError):
    logging.getLogger(__name__).debug("stream reconfigure unavailable")


def pytest_sessionfinish(session, exitstatus):  # args unused; hook signature
    """#435: the abort fires under the interpreter's exit-time flush.

    Re-flushing synchronously here moves the summary's pending output to the
    OS before teardown begins, so the race window starts already drained."""
    import time

    time.sleep(0.05)
    try:
        del session, exitstatus
        sys.stdout.flush()
        sys.stderr.flush()
    except (ValueError, OSError):
        logging.getLogger(__name__).debug("final flush unavailable")


@pytest.fixture(autouse=True)
def clamp_pbkdf2_iterations(request, monkeypatch):
    """Hold caller-key hashing at a 1_000-iteration work factor in tests.

    The real 210k derivation is the dominant cost of every keyed-key test;
    the real path is covered by the ``real_pbkdf2``-marked test."""
    if request.node.get_closest_marker("real_pbkdf2"):
        return
    from crawlutron.storage import caller_key_store

    monkeypatch.setattr(caller_key_store, "PBKDF2_ITERATIONS", 1_000)


def real_pbkdf2_iterations() -> int:
    """The unclamped work factor as captured at conftest import."""
    import tests.conftest as conftest

    assert conftest._REAL_PBKDF2_ITERATIONS is not None
    return conftest._REAL_PBKDF2_ITERATIONS


@pytest.fixture(autouse=True)
def fast_sleeps(request):
    """Zero ``time.sleep`` and ``asyncio.sleep`` unless the test opts out.

    Timing-true tests carry the ``real_sleeps`` marker instead. The patch is a
    stable dispatcher (module level, below): never a ``mock.patch`` whose
    restore a sibling monkeypatch can capture and re-install."""
    _SLEEP_MODE["fast"] = not request.node.get_closest_marker("real_sleeps")
    yield
    _SLEEP_MODE["fast"] = False


@pytest.fixture(autouse=True)
def stub_robots_fetch(request, monkeypatch):
    """No external robots.txt fetch in tests: the decode default is the door."""
    if request.node.get_closest_marker("real_cffi"):
        return
    import crawlutron.transport.fetchers as fetchers_module
    import crawlutron.transport.robots as robots_module

    real_decode = robots_module.default_fetch_and_decode

    def _loopback(url: object) -> bool:
        host = str(getattr(url, "host", "") or "").lower()
        return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")

    async def _no_external_robots(fetcher, url, headers, timeout_seconds):
        # Doubles and loopback fetches are the caller's own wire: pass through.
        # An external production-fetcher name gets RFC 9309's no-answer: disallow.
        if fetcher is not fetchers_module.default_http_fetcher or _loopback(url):
            return await real_decode(fetcher, url, headers, timeout_seconds)
        return None, None

    # Line-level door: the decode default is bound exactly when the caller
    # supplied none, so this intercepts every real-dial path; seam stubs pass.
    monkeypatch.setattr(robots_module, "default_fetch_and_decode", _no_external_robots)


@pytest.fixture(autouse=True)
def stub_cffi_fetcher(request, monkeypatch):
    """Replace the real curl_cffi TLS-impersonation fetcher in tests.

    Stream-mode curl_cffi busy-spins the GIL under fast_sleeps; real cffi
    runs only in subprocess-isolated and ``real_cffi``-marked tests."""
    if request.node.get_closest_marker("real_cffi"):
        return
    import crawlutron.transport.fetchers as fetchers_module

    async def _stub_cffi_fetcher(url, headers, timeout_seconds, max_content_length=0):
        raise ConnectionError("curl_cffi stubbed in tests (mark test with real_cffi to opt in)")

    monkeypatch.setattr(fetchers_module, "impersonating_fetcher", lambda: _stub_cffi_fetcher)


@pytest.fixture(autouse=True, scope="session")
def _cleanup_extraction_executors():
    """Shutdown the document extraction process pool at end of test session."""
    yield
    import asyncio

    from crawlutron.extraction.content import shutdown_doc_extraction_executor

    asyncio.run(shutdown_doc_extraction_executor())


@pytest.fixture(autouse=True)
def _document_pool_per_test(monkeypatch):
    """Each test that touches the document pool gets a fresh one, built lazily.

    The pool is a process global whose teardown is terminal: a test that ran
    the lifespan closed it for every later test in the same worker. Building
    it eagerly for every test cost a spawn+teardown per test (#615), so the
    patched global is a lazy proxy: the real pool is built on first attribute
    use and torn down at fixture finalization if it was ever built.
    """
    import crawlutron.extraction.content as ext_mod
    import crawlutron.extraction.documents as docex
    from crawlutron.extraction.doc_pool import DocExtractionPool

    built: list[DocExtractionPool] = []
    build_lock = threading.Lock()

    class _LazyDocPool:
        def __getattr__(self, name):
            with build_lock:
                if not built:
                    built.append(docex.build_doc_pool())
            return getattr(built[0], name)

    proxy = _LazyDocPool()
    monkeypatch.setattr(docex, "DOC_POOL", proxy, raising=False)
    monkeypatch.setattr(ext_mod, "DOC_POOL", proxy, raising=False)
    yield
    if built:
        # The pool this test used, and any worker it spawned, dies with the test.
        built[0].teardown()


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


def _clear_global_http_state() -> None:
    """Reset every registered process-state owner through the one seam (#621).

    Dead-loop-bound pooled clients leak sockets, cookies and cache state across
    tests; the close is loop-aware (drop for GC) to avoid running a second loop."""
    import asyncio

    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()
    # The real app's shutdown event is module-global and shared by every ASGI test.
    service_app().state.shutdown_event = asyncio.Event()


_MODULE_GLOBAL_RESETS = (_clear_global_http_state,)


@pytest.fixture(autouse=True)
async def _reset_pooled_sockets_on_the_live_loop():
    """Release the pooled HTTP clients while the test's loop still runs.

    The pooled client is loop-bound: a reset that runs after pytest-asyncio
    closed the loop cannot legally close its transports (a foreign-loop close
    leaves them "closing" forever -- the unraisable __del__ flake family).
    Closing on the owning loop before it dies is the structural fix; the sync
    reset below stays as the safety net for stateless owners."""
    yield
    from crawlutron.runtime import process_state
    from crawlutron.transport import pooled_clients

    process_state.reset_all_process_state()
    await pooled_clients.drain_pending_client_closes()


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
    from crawlutron.api import job_runner
    from tests._app_factory import service_app

    app = service_app()
    if app is not None:
        app.state.shutdown_event = asyncio.Event()
    yield
    await job_runner.shutdown_job_runner()
    job_runner.reset_execute_crawl()
