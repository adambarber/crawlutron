import pytest

pytestmark = pytest.mark.browser

import asyncio
import time

import app.crawl.strategies.browser as browser_mod
from app.crawl.strategies.browser import _make_ssrf_route_handler


class MockRequest:
    def __init__(self, url):
        self.url = url


class MockRoute:
    def __init__(self):
        self.aborted = False
        self.continued = False
        self.continued_url = None

    async def abort(self):
        self.aborted = True

    async def continue_(self, url=None, headers=None):
        self.continued = True
        self.continued_url = url


def slow_build_pinned_request(url, headers):
    time.sleep(0.05)  # Blocking call - would block event loop if run directly
    return ("http://1.2.3.4:80/", {"Host": "example.com"})


async def test_ssrf_route_handler_runs_dns_check_in_thread_pool(monkeypatch, real_sleeps):
    """The route handler should run _build_pinned_request via asyncio.to_thread to avoid
    blocking the event loop with synchronous DNS lookups (Fix 2 + F7/F8 pinning).

    ``real_sleeps`` so the blocking sleep is a true delay (not no-op'd).
    """
    # Track which thread the check runs on
    check_thread_id = None

    def blocking_build_pinned_request(url, headers):
        nonlocal check_thread_id
        import threading

        check_thread_id = threading.get_ident()
        time.sleep(0.05)  # Blocking call - would block event loop if run directly
        return ("http://1.2.3.4:80/", {"Host": "example.com"})

    monkeypatch.setattr(browser_mod, "_build_pinned_request", blocking_build_pinned_request)

    handler = _make_ssrf_route_handler()

    route = MockRoute()
    request = MockRequest("http://example.com/")

    # Run handler - should complete and run the blocking check in a thread
    await handler(route, request)

    assert route.aborted is False
    assert route.continued is True
    # The check should have run in a different thread (thread pool)
    assert check_thread_id is not None
    # Main thread ID
    import threading

    assert check_thread_id != threading.main_thread().ident


async def test_ssrf_route_handler_concurrent_requests_dont_block_each_other(monkeypatch, real_sleeps):
    """Multiple concurrent route handler calls should not block each other.

    ``real_sleeps`` so the concurrency assertion actually measures real timing.
    """
    start_times = []
    end_times = []

    monkeypatch.setattr(browser_mod, "_build_pinned_request", slow_build_pinned_request)

    handler = _make_ssrf_route_handler()

    async def call_handler(i):
        route = MockRoute()
        request = MockRequest(f"http://example.com/{i}")
        start_times.append(time.monotonic())
        await handler(route, request)
        end_times.append(time.monotonic())

    # Launch 5 concurrent handlers
    await asyncio.gather(*[call_handler(i) for i in range(5)])

    # If properly using thread pool, all 5 should run concurrently in threads
    # and total elapsed time should be ~50ms, not 250ms (5 * 50ms)
    total_time = max(end_times) - min(start_times)
    # Allow some overhead, but should be well under 200ms
    assert total_time < 0.2, f"Concurrent handlers took {total_time:.3f}s, expected < 0.2s"


# ============================================================
# Finding 1: Route handler must PIN subresources to validated IP
# to prevent DNS-rebinding TOCTOU (SSRF)
# ============================================================


async def test_ssrf_route_handler_pins_safe_subresource(monkeypatch):
    """Safe subresource requests should be pinned to validated IP and continued
    with the pinned URL and original Host header."""
    handler = _make_ssrf_route_handler()

    # Mock _build_pinned_request (sync - runs in thread pool via asyncio.to_thread)
    def mock_build_pinned_request(url, headers):
        # Simulate DNS resolution + pinning
        return ("http://1.2.3.4:80/path", {"Host": "example.com"})

    monkeypatch.setattr(browser_mod, "_build_pinned_request", mock_build_pinned_request)

    class MockRequest:
        def __init__(self, url):
            self.url = url

    class MockRoute:
        def __init__(self):
            self.aborted = False
            self.continued_url = None
            self.continued_headers = None

        async def abort(self):
            self.aborted = True

        async def continue_(self, url=None, headers=None):
            self.continued_url = url
            self.continued_headers = headers

    route = MockRoute()
    request = MockRequest("http://example.com/path")
    await handler(route, request)

    assert route.aborted is False
    assert route.continued_url == "http://1.2.3.4:80/path"
    assert route.continued_headers == {"Host": "example.com"}


async def test_ssrf_route_handler_aborts_unsafe_subresource(monkeypatch):
    """Unsafe subresource requests (resolving to private IP) should be aborted."""
    handler = _make_ssrf_route_handler()

    # Mock _build_pinned_request (sync) to raise ValueError for unsafe target
    def mock_build_pinned_request(url, headers):
        raise ValueError("SSRF protection: hostname resolves to private IP")

    monkeypatch.setattr(browser_mod, "_build_pinned_request", mock_build_pinned_request)

    route = MockRoute()
    request = MockRequest("http://127.0.0.1:9/")
    await handler(route, request)

    assert route.aborted is True
    assert route.continued is False


async def test_ssrf_route_handler_runs_build_pinned_request_in_thread_pool(monkeypatch, real_sleeps):
    """The route handler should run _build_pinned_request via asyncio.to_thread
    to avoid blocking the event loop with synchronous DNS lookups."""
    check_thread_id = None

    def blocking_build_pinned_request(url, headers):
        nonlocal check_thread_id
        import threading

        check_thread_id = threading.get_ident()
        time.sleep(0.05)  # Blocking DNS
        return ("http://1.2.3.4:80/", {"Host": "example.com"})

    monkeypatch.setattr(browser_mod, "_build_pinned_request", blocking_build_pinned_request)

    handler = _make_ssrf_route_handler()

    route = MockRoute()
    request = MockRequest("http://example.com/")
    await handler(route, request)

    assert route.continued_url == "http://1.2.3.4:80/"
    assert check_thread_id is not None
    import threading

    assert check_thread_id != threading.main_thread().ident


async def test_ssrf_route_handler_concurrent_pinning_dont_block(monkeypatch, real_sleeps):
    """Multiple concurrent pinning operations should not block each other."""
    start_times = []
    end_times = []

    monkeypatch.setattr(browser_mod, "_build_pinned_request", slow_build_pinned_request)

    handler = _make_ssrf_route_handler()

    class MockRequest:
        def __init__(self, url):
            self.url = url

    class MockRoute:
        def __init__(self):
            self.continued = False

        async def continue_(self, **kwargs):
            self.continued = True

    async def call_handler(i):
        route = MockRoute()
        request = MockRequest(f"http://example.com/{i}")
        start_times.append(time.monotonic())
        await handler(route, request)
        end_times.append(time.monotonic())

    await asyncio.gather(*[call_handler(i) for i in range(5)])

    total_time = max(end_times) - min(start_times)
    assert total_time < 0.2, f"Concurrent handlers took {total_time:.3f}s, expected < 0.2s"


class _Req:
    def __init__(self, url):
        self.url = url


class _Route:
    def __init__(self):
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self, **k):
        self.continued = True


async def test_ssrf_route_handler_fails_closed_on_unexpected_errors(monkeypatch):
    """Non-ValueError resolver failures (DNS timeout etc.) must abort, not pass."""
    handler = _make_ssrf_route_handler()

    def boom(url, headers):
        raise TimeoutError("dns lookup timed out")

    monkeypatch.setattr(browser_mod, "_build_pinned_request", boom)

    route = _Route()
    await handler(route, _Req("http://example.com/"))
    assert route.aborted is True and route.continued is False


async def test_ssrf_route_handler_allows_non_network_schemes():
    """data:/blob:/about: requests never touch DNS; they must pass through."""
    handler = _make_ssrf_route_handler()

    for scheme_url in ("data:text/html;base64,PGI+", "blob:https://x/y", "about:blank"):
        route = _Route()
        await handler(route, _Req(scheme_url))
        assert route.continued is True and route.aborted is False, scheme_url


async def test_ws_guard_closes_internal_targets(monkeypatch):
    """WebSocket upgrades to private hosts are closed; public ones pass."""
    from _testkit import block_private_targets

    from app.crawl.strategies.browser import _make_ssrf_websocket_handler

    block_private_targets(monkeypatch)
    handler = _make_ssrf_websocket_handler()

    class Ws:
        def __init__(self, url):
            self.url = url
            self.closed = False

        def close(self):
            self.closed = True

    internal = Ws("ws://127.0.0.1:9000/socket")
    await handler(internal)
    assert internal.closed is True, "internal WS not closed"
