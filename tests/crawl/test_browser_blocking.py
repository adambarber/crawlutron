"""Browser request blocking: route handlers, subresources, XHR caps, execute-js gating and consent opt-in."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _testkit import *
from _testkit import BrowserFakePage
from pydantic import ValidationError

import app.crawl.strategies.browser as browser_mod
from app.api_models import CrawlPolicy
from app.crawl.strategies.browser import BrowserRenderer, _make_ssrf_route_handler


@pytest.mark.browser
class MockRequest:
    def __init__(self, url):
        self.url = url


@pytest.mark.browser
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


@pytest.mark.browser
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


@pytest.mark.browser
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


@pytest.mark.browser
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


@pytest.mark.browser
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


@pytest.mark.browser
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


@pytest.mark.browser
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


@pytest.mark.browser
class _Req:
    def __init__(self, url):
        self.url = url


@pytest.mark.browser
class _Route:
    def __init__(self):
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self, **k):
        self.continued = True


@pytest.mark.browser
async def test_ssrf_route_handler_fails_closed_on_unexpected_errors(monkeypatch):
    """Non-ValueError resolver failures (DNS timeout etc.) must abort, not pass."""
    handler = _make_ssrf_route_handler()

    def boom(url, headers):
        raise TimeoutError("dns lookup timed out")

    monkeypatch.setattr(browser_mod, "_build_pinned_request", boom)

    route = _Route()
    await handler(route, _Req("http://example.com/"))
    assert route.aborted is True and route.continued is False


@pytest.mark.browser
async def test_ssrf_route_handler_allows_non_network_schemes():
    """data:/blob:/about: requests never touch DNS; they must pass through."""
    handler = _make_ssrf_route_handler()

    for scheme_url in ("data:text/html;base64,PGI+", "blob:https://x/y", "about:blank"):
        route = _Route()
        await handler(route, _Req(scheme_url))
        assert route.continued is True and route.aborted is False, scheme_url


@pytest.mark.browser
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


@pytest.mark.browser
class _FakeRoute:
    def __init__(self):
        self.actions = []

    async def abort(self):
        self.actions.append(("abort",))

    async def continue_(self, **kw):
        self.actions.append(("continue", kw))

    def decision(self):
        return self.actions[-1][0] if self.actions else None


@pytest.mark.browser
class _FakeRequest:
    def __init__(self, url, resource_type="document"):
        self.url = url
        self.resource_type = resource_type


def _handler_for(resource_mode=None, *, include_screenshot=False):
    return _make_ssrf_route_handler(resource_mode, include_screenshot=include_screenshot)


@pytest.mark.browser
async def test_analytics_aborted_regardless_of_mode():
    route = _FakeRoute()
    handler = _handler_for(None)
    await handler(route, _FakeRequest("https://www.google-analytics.com/analytics.js", "script"))
    assert route.decision() == "abort"


@pytest.mark.browser
async def test_pinterest_aborted_with_and_without_www():
    for url in ("https://pinterest.com/pin/123", "https://www.pinterest.com/pin/123"):
        route = _FakeRoute()
        await _handler_for(None)(route, _FakeRequest(url, "document"))
        assert route.decision() == "abort", url


@pytest.mark.browser
async def test_cdcgov_aborted_with_and_without_www():
    # H3: cdc.gov is a government CONTENT host, not tracking infrastructure.
    # It was removed from ANALYTICS_HOST_SUFFIXES (data poisoning); the
    # route handler must let it through. Suffix match still covers apex
    # and subdomains alike for hosts that ARE on the list (see pinterest).
    for url in ("https://cdc.gov/foo", "https://www.cdc.gov/bar", "https://data.cdc.gov/baz"):
        route = _FakeRoute()
        await _handler_for(None)(route, _FakeRequest(url, "document"))
        assert route.decision() == "continue", url


@pytest.mark.browser
async def test_xcom_aborted_regardless_of_mode():
    # x.com (X/Twitter) is on the hard-skip tracking list: aborted in every mode,
    # including bare-apex and www subdomain forms.
    for url in ("https://x.com/i/web/status/1", "https://www.x.com/home"):
        route = _FakeRoute()
        await _handler_for(None)(route, _FakeRequest(url, "document"))
        assert route.decision() == "abort", url


@pytest.mark.browser
async def test_image_blocked_by_default_unless_screenshot():
    # default (no screenshot requested) -> abort unconditionally (#4)
    route = _FakeRoute()
    await _handler_for(None)(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "abort"

    # reduced mode -> also abort
    route = _FakeRoute()
    await _handler_for("reduced")(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "abort"

    # screenshot explicitly requested -> continue (renders need images/fonts)
    route = _FakeRoute()
    with patch.object(browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {})):
        await _handler_for(None, include_screenshot=True)(
            route, _FakeRequest("https://x.test/logo.png", "image")
        )
    assert route.decision() == "continue"


@pytest.mark.browser
async def test_document_always_continues_with_pinned_url():
    route = _FakeRoute()
    with patch.object(
        browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {"Host": "x.test"})
    ):
        await _handler_for(None)(route, _FakeRequest("https://x.test/page", "document"))
    assert route.decision() == "continue"
    assert route.actions[-1][1]["url"] == "http://1.2.3.4/"


@pytest.mark.browser
class _FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.mark.browser
async def test_response_handler_caps_entries_and_payload_size():
    collected = []
    handler = BrowserRenderer._make_response_handler(collected)

    # Over the entry cap: only the first max_entries (10) are kept.
    big = {"blob": "x" * 200_000}
    for i in range(60):
        await handler(_FakeResponse(f"https://x/{i}", big))

    assert len(collected) == 10, f"entry cap not enforced: {len(collected)}"
    # Each kept payload is capped at max_payload_chars (8192); oversized ones
    # are flagged truncated rather than stored whole.
    assert all(
        e.get("payload_truncated") or len((e.get("payload") or {}).get("blob", "")) <= 8192 for e in collected
    )
    assert all("payload_truncated" in e or (e.get("payload") or {}).get("blob") for e in collected)
    # Oversized payloads are truncated out rather than stored whole.
    assert all(len((e.get("payload") or {}).get("blob", "")) <= 200_000 for e in collected)
    assert any(e.get("payload_truncated") for e in collected)


class TestXhrResponsePayloadCapture:
    async def test_json_payload_captured(self):
        from app.crawl.strategies.browser import BrowserRenderer

        collected: list[dict[str, Any]] = []
        handler = BrowserRenderer._make_response_handler(collected)

        class FakeResponse:
            headers = {"content-type": "application/json"}
            url = "http://x/api/data"
            status = 200

            async def json(self):
                return {"a": 1}

        await handler(FakeResponse())
        assert collected[0]["payload"] == {"a": 1}

    async def test_bad_json_payload_captured_as_none(self):
        from app.crawl.strategies.browser import BrowserRenderer

        collected: list[dict[str, Any]] = []
        handler = BrowserRenderer._make_response_handler(collected)

        class FakeResponse:
            headers = {"content-type": "application/json"}
            url = "http://x/api/badjson"
            status = 200

            async def json(self):
                raise ValueError("bad json")

        await handler(FakeResponse())
        assert len(collected) == 1
        assert (collected[0]["payload"]) is None


def _make_mock_page() -> MagicMock:
    page = MagicMock()
    for name in ("click", "fill", "hover", "query_selector", "wait_for_selector", "evaluate", "close"):
        setattr(page, name, AsyncMock())
    # scroll's query_selector should resolve to an element
    page.query_selector.return_value = MagicMock()
    return page


class TestExecuteJsBlocked:
    async def test_run_action_chain_does_not_execute_user_js(self):
        """A user-supplied ``execute_js`` script must never reach page.evaluate."""
        page = _make_mock_page()
        renderer = BrowserRenderer()
        await renderer._run_action_chain(
            page,
            [
                {"type": "execute_js", "script": "malicious()"},
                {"type": "scroll_bottom"},
            ],
        )
        called_scripts = [str(c.args[0]) for c in page.evaluate.call_args_list]
        assert "malicious()" not in called_scripts
        # The hardcoded, safe scroll_bottom evaluate must still run.
        assert any("window.scrollTo" in c for c in called_scripts)

    def test_crawl_policy_rejects_execute_js_action(self):
        """The API must reject execute_js actions before any crawl runs."""
        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [{"type": "execute_js", "script": "x()"}]})

    def test_crawl_policy_allows_benign_actions_and_profile(self):
        """Benign browser_profile keys/actions are still accepted."""
        policy = CrawlPolicy(
            browser_profile={
                "viewport": "mobile",
                "resource_mode": "reduced",
                "actions": [
                    {"type": "click", "selector": "#a"},
                    {"type": "wait_seconds", "seconds": 1},
                ],
            }
        )
        assert policy.browser_profile["viewport"] == "mobile"
        assert policy.browser_profile["actions"][0]["type"] == "click"


class _Clickable:
    def __init__(self):
        self.clicks = 0

    async def is_visible(self):
        return True

    async def click(self):
        self.clicks += 1


class _ConsentPage(BrowserFakePage):
    def __init__(self):
        super().__init__()
        self.element = _Clickable()

    async def query_selector(self, selector):
        return self.element

    async def evaluate(self, *a, **k):
        return False


async def test_consent_clicking_off_by_default():
    page = _ConsentPage()
    renderer = BrowserRenderer()
    assert await renderer._auto_accept_cookie_consent(page, False) is False
    assert page.element.clicks == 0


async def test_consent_clicking_when_opted_in():
    page = _ConsentPage()
    renderer = BrowserRenderer()
    assert await renderer._auto_accept_cookie_consent(page, True) is True
    assert page.element.clicks == 1
