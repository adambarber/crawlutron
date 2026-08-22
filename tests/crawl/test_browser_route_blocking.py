import asyncio
import time

import pytest

import app.crawl.strategies.browser as browser_mod
from app.crawl.strategies.browser import _make_ssrf_route_handler


async def test_ssrf_route_handler_runs_dns_check_in_thread_pool(monkeypatch, real_sleeps):
    """The route handler should run _is_ssrf_blocked via asyncio.to_thread to avoid
    blocking the event loop with synchronous DNS lookups (Fix 2).

    ``real_sleeps`` so the blocking sleep is a true delay (not no-op'd).
    """
    # Track which thread the check runs on
    check_thread_id = None

    def blocking_is_ssrf_blocked(url):
        nonlocal check_thread_id
        import threading
        check_thread_id = threading.get_ident()
        time.sleep(0.05)  # Blocking call - would block event loop if run directly
        return True

    monkeypatch.setattr(browser_mod, "_is_ssrf_blocked", blocking_is_ssrf_blocked)

    handler = _make_ssrf_route_handler()

    class MockRequest:
        def __init__(self, url):
            self.url = url

    class MockRoute:
        def __init__(self):
            self.aborted = False
            self.continued = False
        async def abort(self):
            self.aborted = True
        async def continue_(self):
            self.continued = True

    route = MockRoute()
    request = MockRequest("http://example.com/")

    # Run handler - should complete and run the blocking check in a thread
    await handler(route, request)

    assert route.aborted is True
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

    def slow_is_ssrf_blocked(url):
        time.sleep(0.05)
        return False  # Allow all

    monkeypatch.setattr(browser_mod, "_is_ssrf_blocked", slow_is_ssrf_blocked)

    handler = _make_ssrf_route_handler()

    class MockRequest:
        def __init__(self, url):
            self.url = url

    class MockRoute:
        def __init__(self):
            self.continued = False
        async def continue_(self):
            self.continued = True

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
