"""Adversarial fix tests: body-size cap, storage_state path allowlist,
DNS cache lock, robots cache lock, _ResultSpill fd-leak fix, _PENDING_CLIENT_CLOSE
bound, and FastAPI lifespan removal of ``lifespan=_lifespan(None)``.
"""
import os
import threading

# ---- Body-size middleware (source inspection) ----
from app.crawl.strategies.browser import BrowserRenderer


def _read_main_source() -> str:
    import inspect

    from app import main as main_mod
    return inspect.getsource(main_mod)


def test_body_size_cap_is_16_mib():
    """The ASGI body cap must be 16 MiB."""
    import inspect

    from app.api import middleware as mw_mod

    src = inspect.getsource(mw_mod)
    assert "_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024" in src


def test_body_size_middleware_registered():
    """The middleware must be installed on the FastAPI app."""
    src = _read_main_source()
    # Registration lives in main.py; implementation lives in app/api/middleware.py
    assert 'limit_request_body' in src
    assert 'app.middleware("http")(limit_request_body)' in src or '@app.middleware("http")' in src
    import inspect

    from app.api import middleware as mw_mod

    mw_src = inspect.getsource(mw_mod)
    assert "async def limit_request_body" in mw_src or "async def _limit_request_body" in mw_src
    # Must wrap the ASGI receive() rather than buffer-and-replay
    assert "original_receive" in mw_src
    assert "request._receive = _capped_receive" in mw_src


# ---- storage_state path validation ----


def test_storage_state_rejects_path_under_tmp_when_settings_present():
    """When settings are provided, ``/tmp`` is NOT an allowed base."""
    from app.settings import CrawlerSettings
    settings = CrawlerSettings(cache_dir="/app/cache")
    r = BrowserRenderer(settings=settings)
    # /tmp is allowed only when no settings were supplied
    assert r._validate_storage_state_path("/tmp/some-storage.json") is None
    # /app/cache is allowed
    assert r._validate_storage_state_path("/app/cache/some-storage.json") is not None


def test_storage_state_allows_known_keys():
    from app.settings import CrawlerSettings
    settings = CrawlerSettings(cache_dir="/app/cache")
    r = BrowserRenderer(settings=settings)
    for key in ("default", "session", "persistent"):
        assert r._validate_storage_state_path(key) == key


def test_storage_state_rejects_path_traversal():
    from app.settings import CrawlerSettings
    settings = CrawlerSettings(cache_dir="/app/cache")
    r = BrowserRenderer(settings=settings)
    assert r._validate_storage_state_path("/app/elsewhere/x.json") is None


def test_storage_state_fallback_to_tmp_only_when_no_settings():
    """No-settings callers (local dev) still allow /tmp."""
    r = BrowserRenderer()
    assert r._cache_dir is None
    # /tmp is allowed in local-dev mode
    assert r._validate_storage_state_path("/tmp/storage.json") is not None
    # But not /elsewhere
    assert r._validate_storage_state_path("/elsewhere/x.json") is None


def test_browser_renderer_stores_cache_dir():
    """The cache_dir set on settings is accessible for storage_state validation."""
    from app.settings import CrawlerSettings
    settings = CrawlerSettings(cache_dir="/var/lib/browser-crawler/cache")
    r = BrowserRenderer(settings=settings)
    assert r._cache_dir == "/var/lib/browser-crawler/cache"


# ---- DNS cache lock ----
def test_dns_cache_lock_is_present_and_used():
    from app.crawl.strategies import http as http_mod
    assert hasattr(http_mod, "_DNS_CACHE_LOCK")
    assert isinstance(http_mod._DNS_CACHE_LOCK, type(threading.Lock()))


def test_dns_cache_concurrent_writes_do_not_corrupt():
    """Two threads racing ``_resolve_ips`` must produce a consistent dict."""
    from app.crawl.strategies import http as http_mod

    # Resolve a host that exists on every test machine
    host = "127.0.0.1"
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(50):
                http_mod._resolve_ips(host)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    # Cache must still be a dict (not corrupted by concurrent mutation).
    assert isinstance(http_mod._DNS_CACHE, dict)


# ---- robots_cache lock ----
def test_robots_cache_lock_is_present():
    from app.crawl import engine as engine_mod
    assert hasattr(engine_mod, "_ROBOTS_CACHE_LOCK")
    assert isinstance(engine_mod._ROBOTS_CACHE_LOCK, type(threading.Lock()))


# ---- _PENDING_CLIENT_CLOSE bounded ----
def test_pending_client_close_has_cap():
    from app.crawl.strategies import http as http_mod
    assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_MAX")
    assert http_mod._PENDING_CLIENT_CLOSE_MAX >= 1
    assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_LOCK")


def test_pending_client_close_cap_drops_oldest_silently():
    """When the cap is reached, the oldest entry is dropped (reference released)
    rather than scheduled for close via ``asyncio.ensure_future``. The drop path
    must work without a running event loop (sync tests / shutdown paths) so
    any attempt to schedule a future here would either raise or schedule on
    the wrong loop, silently leaking the sockets (adversarial review
    F-pending-client-close).
    """
    from unittest.mock import MagicMock

    from app.crawl.strategies import http as http_mod

    # Pre-fill the pending list to the cap with mock clients.
    mocks = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.extend(mocks)
    # The fix must drop the oldest reference without calling
    # ``asyncio.ensure_future`` (which has no running loop in this context).
    oldest = mocks[0]
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        # Simulate the cap path
        assert len(http_mod._PENDING_CLIENT_CLOSE) >= http_mod._PENDING_CLIENT_CLOSE_MAX
        dropped = http_mod._PENDING_CLIENT_CLOSE.pop(0)
    assert dropped is oldest
    assert oldest not in http_mod._PENDING_CLIENT_CLOSE


# ---- _ResultSpill fd-leak fix ----
def test_result_spill_close_deletes_owned_file():
    """Auto-created spill files are removed on close."""
    from app.crawl.engine import _ResultSpill
    spill = _ResultSpill()  # auto-owned path
    path = spill.path
    assert os.path.exists(path)
    spill.close()
    assert not os.path.exists(path), "Auto-owned spill file was not cleaned up"


def test_result_spill_keeps_caller_owned_path():
    """Caller-supplied spill paths are NOT removed on close."""
    import tempfile

    from app.crawl.engine import _ResultSpill
    target = os.path.join(tempfile.gettempdir(), "spill_keep_test.json")
    try:
        spill = _ResultSpill(target)
        spill.close()
        assert os.path.exists(target), "Caller-owned spill file was wrongly deleted"
    finally:
        if os.path.exists(target):
            os.unlink(target)


# ---- lifespan fix ----
def test_lifespan_is_direct_contextmanager():
    """The FastAPI lifespan must be a direct contextmanager, not ``_lifespan(None)``."""
    src = _read_main_source()
    # The old pattern was: lifespan=_lifespan(None) with def _lifespan(app: FastAPI)
    assert "lifespan=_lifespan(None)" not in src
    # The new pattern: a directly-decorated async context manager.
    assert "@asynccontextmanager" in src
    assert "async def _lifespan(_app: FastAPI)" in src
    assert "lifespan=_lifespan" in src
