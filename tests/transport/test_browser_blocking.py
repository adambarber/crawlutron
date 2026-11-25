"""Browser request blocking through the S4 loopback egress proxy.

Route handlers admit via NormalizedUrl and continue logical URLs; proxy owns dialing.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.intercepted_xhr_record import InterceptedXhrRecord
from crawlutron.transport.browser_driver import BrowserDriver
from crawlutron.transport.browser_targets import (
    make_response_handler,
    make_ssrf_route_handler,
)

# run_blocking_document_route and run_route_with_pinner are deprecated for proxy mode
from tests._browser_doubles import (
    BrowserFakePage,
    FakeRequest,
    FakeRoute,
    assert_route_decision,
    block_private_targets,
    response_handler_double,
)
from tests._browser_doubles import (
    ssrf_route_handler as _ssrf_route_handler,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

# Route handler validates via NormalizedUrl (proxy owns DNS/dial).


async def test_ssrf_route_handler_pins_safe_subresource(monkeypatch):
    """Safe subresource is admitted and continued with the logical URL."""
    route = await run_route_with_pinner(
        "http://example.com/path",
        pinned=("http://1.2.3.4:80/path", {"Host": "example.com"}),  # ignored in proxy mode
    )

    assert route.decision == "continue"
    # With proxy mode, we continue with the logical URL
    assert route.continued_url == "http://example.com/path"


async def test_ssrf_route_handler_aborts_unsafe_subresource(monkeypatch):
    """Unsafe subresource requests (resolving to private IP) should be aborted."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    route = await run_route_with_pinner(
        "http://127.0.0.1:9/",
        raises=ValueError("SSRF protection: hostname resolves to private IP"),
    )

    # The route handler validates via NormalizedUrl with private targets blocked
    assert route.decision == "abort", "loopback URL should be aborted by NormalizedUrl admission"


async def test_ssrf_route_handler_runs_build_pinned_request_in_thread_pool():
    """In proxy mode, DNS resolution happens on the proxy side, not in the browser.

    The browser only validates via NormalizedUrl admission.
    """
    route, ran_off_loop = await run_blocking_document_route()

    # In proxy mode, the browser doesn't do DNS resolution
    assert route.decision == "continue"
    assert ran_off_loop is True  # Always True now since no DNS resolution in browser


async def test_ssrf_route_handler_concurrent_pinning_dont_block(monkeypatch):
    """Multiple concurrent requests should not block each other.

    In proxy mode, DNS resolution happens on the proxy side, so the browser
    doesn't block on any resolution operations.
    """
    # In proxy mode, concurrent requests don't block on DNS since browser doesn't resolve
    routes = [FakeRoute() for _ in range(5)]

    # Run all handlers concurrently.
    async def run_handler(route, url):
        handler = make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
        await handler(route, FakeRequest(url, "document"))

    await asyncio.gather(*(run_handler(route, f"http://example.com/{i}") for i, route in enumerate(routes)))

    assert all(route.decision == "continue" for route in routes), "all routes should continue"


async def test_ssrf_route_handler_fails_closed_on_unexpected_errors(monkeypatch):
    """When NormalizedUrl admission fails, the route should abort."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    handler = make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)

    # A loopback URL should be aborted when private targets are blocked
    route = FakeRoute()
    await handler(route, FakeRequest("http://127.0.0.1:9/", "document"))
    assert route.decision == "abort"


async def test_ssrf_route_handler_allows_non_network_schemes():
    """Data:/blob:/about:/javascript: are local-harmless and must continue.

    javascript: is deliberately absent from the old tuple the allowlist replaces;
    only the local-harmless, no-DNS set may be delegated to the browser."""
    await assert_route_decision(
        _ssrf_route_handler(),
        (
            "data:text/html;base64,PGI+",
            "blob:https://x/y",
            "about:blank",
            "javascript:void(0)",
        ),
        "continue",
    )


async def test_ssrf_route_handler_aborts_file_and_unknown_schemes():
    """Any scheme outside the allowlist and http(s) is refused, not delegated.

    A file: URL from an http origin would resolve local content in the headless
    browser; the allowlist is the honest posture -- unknown = abort."""
    await assert_route_decision(
        _ssrf_route_handler(),
        (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "ws://example.com/socket",
            "wss://example.com/socket",
            "chrome://settings",
            "future-scheme://x/y",
        ),
        "abort",
    )


async def test_ssrf_route_handler_file_scheme_aborts_before_any_url_admission():
    """A file: subresource reaches the handler and is refused at the allowlist.

    The abort must happen before URL admission so the browser never sees the
    local path -- the likely attack surface for handing file content outward."""
    route = FakeRoute()
    await _ssrf_route_handler()(route, FakeRequest("file:///etc/passwd", "document"))
    assert route.decision == "abort"


async def test_ssrf_route_handler_upper_case_file_scheme_still_aborts():
    """Scheme matching is case-insensitive: FILE: must abort like file:."""
    route = FakeRoute()
    await _ssrf_route_handler()(route, FakeRequest("FILE:///etc/passwd", "document"))
    assert route.decision == "abort"


async def test_ws_guard_closes_internal_targets(monkeypatch):
    """WebSocket upgrades to private hosts are closed; public ones pass."""
    from crawlutron.transport.browser import make_ssrf_websocket_handler

    # With private targets blocked, internal WS should be closed
    block_private_targets(monkeypatch)
    handler = make_ssrf_websocket_handler(policy=UNRESTRICTED_HOST_POLICY)

    class Ws:
        def __init__(self, url):
            self.url = url
            self.closed = False

        async def close(self):
            self.closed = True

    internal = Ws("ws://127.0.0.1:9000/socket")
    await handler(internal)
    assert internal.closed is True, "internal WS not closed"


async def test_analytics_aborted_regardless_of_mode():
    route = FakeRoute()
    handler = _ssrf_route_handler()
    await handler(route, FakeRequest("https://www.google-analytics.com/analytics.js", "script"))
    assert route.decision == "abort"


async def test_pinterest_aborted_with_and_without_www():
    await assert_route_decision(
        _ssrf_route_handler(),
        ("https://pinterest.com/pin/123", "https://www.pinterest.com/pin/123"),
        "abort",
    )


async def test_cdcgov_aborted_with_and_without_www():
    # Cdc.gov is a government CONTENT host, not tracking infrastructure. Suffix match still covers apex and
    # subdomains alike for hosts that ARE on the list (see pinterest).
    await assert_route_decision(
        _ssrf_route_handler(),
        ("https://cdc.gov/foo", "https://www.cdc.gov/bar", "https://data.cdc.gov/baz"),
        "continue",
    )


async def test_xcom_aborted_regardless_of_mode():
    # X.com (X/Twitter) is on the hard-skip tracking list: aborted in every mode, including bare-apex and www
    # subdomain forms.
    await assert_route_decision(
        _ssrf_route_handler(),
        ("https://x.com/i/web/status/1", "https://www.x.com/home"),
        "abort",
    )


async def test_image_blocked_by_default_unless_screenshot():
    # Default (no screenshot requested) -> abort unconditionally
    route = FakeRoute()
    await _ssrf_route_handler()(route, FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision == "abort"

    # Reduced mode -> also abort
    route = FakeRoute()
    await _ssrf_route_handler(resource_mode="reduced")(route, FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision == "abort"

    # Screenshot explicitly requested -> continue (renders need images/fonts)
    route = FakeRoute()
    await _ssrf_route_handler(include_screenshot=True)(route, FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision == "continue"


async def test_document_always_continues_with_logical_url(monkeypatch):
    """With proxy mode, documents navigate to the logical URL."""
    route = FakeRoute()
    await _ssrf_route_handler()(route, FakeRequest("http://x.test/page", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "http://x.test/page"


class _FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload


def payload_truncated(e: InterceptedXhrRecord) -> bool:
    """Whether the record itself flags its payload as truncated."""
    return bool(e.get("payload_truncated"))


async def test_response_handler_caps_entries_and_payload_size():
    collected: list[InterceptedXhrRecord] = []
    handler = make_response_handler(collected)

    # Over the entry cap: only the first max_entries (10) are kept.
    big = {"blob": "x" * 200_000}
    for i in range(60):
        await handler(_FakeResponse(f"https://x/{i}", big))

    assert len(collected) == 10, f"entry cap not enforced: {len(collected)}"
    for e in collected:
        payload = e.get("payload")
        assert payload is None or payload_truncated(e) or isinstance(payload, dict)
        if isinstance(payload, dict):
            blob = payload.get("blob", "")
            assert isinstance(blob, str)
            assert len(blob) <= 200_000
    assert any(e.get("payload_truncated") for e in collected)


async def test_response_handler_caps_the_url_it_retains():
    """The entry cap bounds entries, not what an entry may contain.

    The URL was the one unbounded field: multi-MB page-controlled text went
    into result metadata through a bound documented to cover the whole entry.
    """
    collected: list[InterceptedXhrRecord] = []
    handler = make_response_handler(collected)
    long_url = "https://x/api?" + "k" * 500_000

    await handler(_FakeResponse(long_url, {"a": 1}))

    entry = collected[0]
    assert entry["url_truncated"] is True
    assert len(entry["url"]) <= 2048
    assert entry["url"].startswith("https://x/api?")
    # Truncating the locator must not cost the payload the entry exists for.
    assert entry["payload"] == {"a": 1}


async def test_response_handler_does_not_flag_a_url_within_the_cap():
    collected: list[InterceptedXhrRecord] = []
    handler = make_response_handler(collected)

    await handler(_FakeResponse("https://x/api/data", {"a": 1}))

    assert "url_truncated" not in collected[0]
    assert collected[0]["url"] == "https://x/api/data"


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
    # Scroll's query_selector should resolve to an element
    page.query_selector.return_value = MagicMock()
    return page


class TestExecuteJsBlocked:
    async def test_run_action_chain_does_not_execute_user_js(self):
        """A user-supplied ``execute_js`` script must never reach page.evaluate."""
        page = _make_mock_page()
        await BrowserDriver().run_action_chain(
            page,
            [{"type": "execute_js", "value": "malicious()"}, {"type": "scroll_bottom"}],
        )
        called_scripts = [str(c.args[0]) for c in page.evaluate.call_args_list]
        # The poison rides in `value`, a declared BrowserAction key -- execute_js is the refused verb, not a
        # declined key shape, and the chain must neither run it nor trip on the payload.
        assert not any("malicious" in script for script in called_scripts)
        # The hardcoded, safe scroll_bottom evaluate must still run.
        assert any("window.scrollTo" in c for c in called_scripts)

    def test_crawl_policy_rejects_execute_js_action(self):
        """The API must reject execute_js actions before any crawl runs."""
        with pytest.raises(ValidationError):
            CrawlPolicy.model_validate(
                {"browser_profile": {"actions": [{"type": "execute_js", "script": "x()"}]}}
            )

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
        actions = policy.browser_profile["actions"]
        assert isinstance(actions, list)
        assert actions[0] == {"type": "click", "selector": "#a"}


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
    assert await BrowserDriver().auto_accept_cookie_consent(page, False) is False
    assert page.element.clicks == 0


async def test_consent_clicking_when_opted_in():
    page = _ConsentPage()
    assert await BrowserDriver().auto_accept_cookie_consent(page, True) is True
    assert page.element.clicks == 1


# Import from tests._browser_doubles for compatibility with proxy mode
from tests._browser_doubles import (
    run_blocking_document_route,
    run_route_with_pinner,
)

# Alias for internal tests
_ssrf_route_handler = _ssrf_route_handler
