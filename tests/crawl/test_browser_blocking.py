"""Browser request blocking: route handlers, subresources, XHR caps, execute-js gating and consent opt-in."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _browser_doubles import (
    BrowserFakePage,
    FakeRequest,
    FakeRoute,
    assert_continued,
    assert_route_decision,
    response_handler_double,
    run_blocking_document_route,
    run_route_with_pinner,
    ssrf_route_handler,
)
from _policy_factories import UNRESTRICTED_HOST_POLICY
from _timing_harnesses import HANG_GUARD_SECONDS
from pydantic import ValidationError

import app.crawl.strategies.browser as browser_mod
from app.api_models import CrawlPolicy
from app.crawl.strategies.browser import BrowserRenderer, _make_ssrf_route_handler


class _BlockingResolver:
    """A synchronous, deliberately loop-blocking stand-in for the DNS resolver.

    The instrument is a ``Barrier``, not a stopwatch. Five concurrent route
    handlers each enter the resolver and wait for all five to be there: the
    barrier can only be satisfied by five resolvers running at the same time.
    The regression this pins is the resolver running ON the event loop, and in
    that case the first caller wedges the loop, the other four never arrive,
    the barrier times out, and the test fails naming exactly that -- instead of
    a 0.2s stopwatch guessing whether five 0.05s sleeps fit inside it and
    passing on a slow box, failing on a fast one.
    """

    def __init__(self, parties: int, timeout: float) -> None:
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.broken: list[str] = []

    def build(self, url, headers, *, policy):
        del url, headers, policy
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            self.broken.append("not all resolvers were in flight at once")
        return ("http://1.2.3.4:80/", {"Host": "example.com"})


async def test_ssrf_route_handler_runs_dns_check_in_thread_pool(monkeypatch):
    """The route handler runs ``_build_pinned_request`` off the event loop.

    Asserted on thread identity, which is stronger than any stopwatch: a
    synchronous DNS lookup that ran on the loop would report the main thread's
    id no matter how long it took.
    """
    route, ran_off_loop = await run_blocking_document_route(monkeypatch, browser_mod)

    assert_continued(route)
    assert ran_off_loop


# ============================================================
# Finding 1: Route handler must PIN subresources to validated IP
# to prevent DNS-rebinding TOCTOU (SSRF)
# ============================================================


async def test_ssrf_route_handler_pins_safe_subresource(monkeypatch):
    """Safe subresource requests should be pinned to validated IP and continued
    with the pinned URL and original Host header."""
    route = await run_route_with_pinner(
        monkeypatch,
        "http://example.com/path",
        pinned=("http://1.2.3.4:80/path", {"Host": "example.com"}),
    )

    assert route.decision == "continue"
    assert route.continued_url == "http://1.2.3.4:80/path"
    assert route.continued_headers == {"Host": "example.com"}


async def test_ssrf_route_handler_aborts_unsafe_subresource(monkeypatch):
    """Unsafe subresource requests (resolving to private IP) should be aborted."""
    route = await run_route_with_pinner(
        monkeypatch,
        "http://127.0.0.1:9/",
        raises=ValueError("SSRF protection: hostname resolves to private IP"),
    )

    assert route.decision == "abort"


async def test_ssrf_route_handler_runs_build_pinned_request_in_thread_pool(monkeypatch):
    """The route handler should run _build_pinned_request via asyncio.to_thread
    to avoid blocking the event loop with synchronous DNS lookups."""
    route, ran_off_loop = await run_blocking_document_route(monkeypatch, browser_mod)

    assert_continued(route, url="http://1.2.3.4:80/")
    assert ran_off_loop


async def test_ssrf_route_handler_concurrent_pinning_dont_block(monkeypatch):
    """Multiple concurrent pinning operations must not block each other."""
    resolver = _BlockingResolver(parties=5, timeout=HANG_GUARD_SECONDS)
    monkeypatch.setattr(browser_mod, "_build_pinned_request", resolver.build)
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)

    routes = [FakeRoute() for _ in range(5)]
    await asyncio.gather(
        *(
            handler(route, FakeRequest(f"http://example.com/{i}", "document"))
            for i, route in enumerate(routes)
        )
    )

    assert not resolver.broken, (
        "the five route handlers did not have all five DNS resolutions in flight "
        "at once: the synchronous resolver is being run on the event loop, so "
        "the first caller wedges the loop and the rest can never start"
    )
    assert all(route.decision == "continue" for route in routes)


async def test_ssrf_route_handler_fails_closed_on_unexpected_errors(monkeypatch):
    """Non-ValueError resolver failures (DNS timeout etc.) must abort, not pass."""
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)

    def boom(url, headers, *, policy):
        raise TimeoutError("dns lookup timed out")

    monkeypatch.setattr(browser_mod, "_build_pinned_request", boom)

    route = FakeRoute()
    await handler(route, FakeRequest("http://example.com/", "document"))
    assert route.decision == "abort"


async def test_ssrf_route_handler_allows_non_network_schemes():
    """data:/blob:/about: requests never touch DNS; they must pass through."""
    await assert_route_decision(
        ssrf_route_handler(),
        ("data:text/html;base64,PGI+", "blob:https://x/y", "about:blank"),
        "continue",
    )


async def test_ws_guard_closes_internal_targets(monkeypatch):
    """WebSocket upgrades to private hosts are closed; public ones pass."""
    from _assertions import block_private_targets

    from app.crawl.strategies.browser import _make_ssrf_websocket_handler

    block_private_targets(monkeypatch)
    handler = _make_ssrf_websocket_handler(policy=UNRESTRICTED_HOST_POLICY)

    class Ws:
        def __init__(self, url):
            self.url = url
            self.closed = False

        def close(self):
            self.closed = True

    internal = Ws("ws://127.0.0.1:9000/socket")
    await handler(internal)
    assert internal.closed is True, "internal WS not closed"


async def test_analytics_aborted_regardless_of_mode():
    route = FakeRoute()
    handler = ssrf_route_handler()
    await handler(route, FakeRequest("https://www.google-analytics.com/analytics.js", "script"))
    assert route.decision == "abort"


async def test_pinterest_aborted_with_and_without_www():
    await assert_route_decision(
        ssrf_route_handler(),
        ("https://pinterest.com/pin/123", "https://www.pinterest.com/pin/123"),
        "abort",
    )


async def test_cdcgov_aborted_with_and_without_www():
    # H3: cdc.gov is a government CONTENT host, not tracking infrastructure.
    # It was removed from ANALYTICS_HOST_SUFFIXES (data poisoning); the
    # route handler must let it through. Suffix match still covers apex
    # and subdomains alike for hosts that ARE on the list (see pinterest).
    await assert_route_decision(
        ssrf_route_handler(),
        ("https://cdc.gov/foo", "https://www.cdc.gov/bar", "https://data.cdc.gov/baz"),
        "continue",
    )


async def test_xcom_aborted_regardless_of_mode():
    # x.com (X/Twitter) is on the hard-skip tracking list: aborted in every mode,
    # including bare-apex and www subdomain forms.
    await assert_route_decision(
        ssrf_route_handler(),
        ("https://x.com/i/web/status/1", "https://www.x.com/home"),
        "abort",
    )


async def test_image_blocked_by_default_unless_screenshot():
    # default (no screenshot requested) -> abort unconditionally (#4)
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision == "abort"

    # reduced mode -> also abort
    route = FakeRoute()
    await ssrf_route_handler("reduced")(route, FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision == "abort"

    # screenshot explicitly requested -> continue (renders need images/fonts)
    route = FakeRoute()
    with patch.object(browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {})):
        await ssrf_route_handler(include_screenshot=True)(
            route, FakeRequest("https://x.test/logo.png", "image")
        )
    assert route.decision == "continue"


async def test_document_always_continues_with_pinned_url():
    route = FakeRoute()
    with patch.object(
        browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {"Host": "x.test"})
    ):
        await ssrf_route_handler()(route, FakeRequest("http://x.test/page", "document"))
    assert route.decision == "continue"
    assert route.actions[-1][1]["url"] == "http://1.2.3.4/"


class _FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload


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
        collected, handler, response = response_handler_double("http://x/api/data", {"a": 1})

        await handler(response)
        assert collected[0]["payload"] == {"a": 1}

    async def test_bad_json_payload_captured_as_none(self):
        collected, handler, response = response_handler_double(
            "http://x/api/badjson", error=ValueError("bad json")
        )

        await handler(response)
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
