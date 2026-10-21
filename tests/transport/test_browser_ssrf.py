import socket
import threading
import time

import pytest

import app.transport.document as doc_mod
from app.transport.browser import make_ssrf_route_handler, resolve_browser_target
from app.transport.pinned_transport import build_pinned_request, build_pinned_request_async
from tests._assertions import block_private_targets
from tests._browser_doubles import (
    BrowserFakePage,
    FakeRequest,
    FakeRoute,
    build_pinned_renderer,
    pinned_page_renderer,
    render_bare,
    render_pinned,
    stub_pinned_request,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

# -------------------------------------------------------------------------- document.py: SSRF gate on urlopen
# --------------------------------------------------------------------------


async def test_document_extractor_blocks_internal_url(monkeypatch):
    # The document fetch must refuse a loopback target, and refuse it loudly -- a refused target is not the
    # same outcome as an empty PDF.
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        doc_mod.default_document_text_extractor(canonical("http://127.0.0.1:9/secret.pdf"))


async def test_document_extractor_invokes_ssrf_check(monkeypatch):
    """The document fetch must gate the URL before any socket opens.

    The gate IS the pin (#383): the extractor swallows every failure, so the
    recording resolver entry -- not an escaping sentinel -- is the observable.
    """
    from unittest.mock import patch

    from tests._transport_doubles import MockResponse, scripted_transport_client

    seen = {}

    def fake_resolve(host, *args, **kwargs):
        seen["url"] = host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolve)
    with patch(
        "app.transport.http_client_pool.get_async_http_client",
        return_value=scripted_transport_client([MockResponse(200, {"content-type": "application/pdf"}, b"")]),
    ):
        doc_mod.default_document_text_extractor(canonical("http://example.com/x.pdf"))
    assert seen.get("url") == "example.com"


# -------------------------------------------------------------------------- browser.py helpers: pinning +
# post-navigation re-validation --------------------------------------------------------------------------


async def test_resolve_browser_target_pins_ip_and_preserves_host():
    pinned_url, headers = await resolve_browser_target(
        canonical("http://example.com/path"),
        pin=stub_pinned_request(("http://1.2.3.4:80/path", {"Host": "example.com"})),
    )
    assert pinned_url == "http://1.2.3.4:80/path"
    assert headers == {"Host": "example.com"}


async def test_browser_pin_resolve_does_not_run_on_the_event_loop(monkeypatch):
    """Blocking DNS must not happen on the loop thread, and this is the render path.

    Every rendered page is pinned before navigation, so a resolve on the loop
    stalls every in-flight crawl -- and the pin builder is what got fixed.
    """
    block_private_targets(monkeypatch)
    loop_thread = threading.get_ident()
    resolving_threads: list[int] = []

    def _getaddrinfo(host, port, *args, **kwargs):
        resolving_threads.append(threading.get_ident())
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)

    await resolve_browser_target(
        canonical("http://example.com/path"),
        pin=build_pinned_request_async,
    )

    assert resolving_threads, "no resolver call was observed, so the invariant was not exercised"
    assert all(ident != loop_thread for ident in resolving_threads), (
        "navigation pinning resolved DNS on the event loop thread, which blocks "
        f"every other crawl in the process once per rendered page (loop thread "
        f"{loop_thread}, resolver threads {resolving_threads})"
    )


async def test_render_navigates_to_pinned_ip_with_host_header(monkeypatch):
    page, result = await render_pinned(monkeypatch)
    # Navigate to the pinned IP, not the hostname (DNS-rebinding safe).
    assert page.goto_calls[0][0] == "http://1.2.3.4:80/"
    # Should use set_extra_http_headers, NOT extra_http_headers param; the pinned Host is cleared right after
    # navigation.
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}, {}]
    assert result.status == "success"


async def test_render_rejects_internal_redirect_target(monkeypatch):
    page, renderer = pinned_page_renderer(monkeypatch)
    page.url = "http://127.0.0.1:9/"  # site redirected the browser to loopback
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        await render_bare(renderer)


# -------------------------------------------------------------------------- route-level guard aborts
# SSRF-unsafe requests (subresources + redirects)


async def test_ssrf_route_handler_aborts_unsafe_and_continues_safe(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    handler = make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY, pin=build_pinned_request)

    bad = FakeRoute()
    await handler(bad, FakeRequest("http://127.0.0.1:9/", "document"))
    assert bad.decision == "abort"

    good = FakeRoute()
    await handler(good, FakeRequest("http://1.2.3.4/", "document"))
    assert good.decision == "continue"


async def test_render_registers_ssrf_route_guard(monkeypatch):
    page, renderer = pinned_page_renderer(monkeypatch)
    await render_bare(renderer)
    # The SSRF route guard must be registered on the page.
    assert any(pattern == "**/*" for pattern, _ in page.route_calls)


# -------------------------------------------------------------------------- #405: the browser's SSRF
# resolve waits are budget-bounded and fail as a classifiable resolution failure


def _hanging_resolver(seconds: float):
    def resolve(host, *args, **kwargs):
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", kwargs.get("port", 80)))]

    return resolve


def _hanging_pin():
    def pin(url, headers):
        time.sleep(7)
        return ("http://1.2.3.4/", {})

    return pin


async def _handler_aborts_within_budget(handler) -> None:
    """One hang through the handler: it must abort fail-closed and fast, not wait
    out the pin's own 7s sleep."""
    route = FakeRoute()
    started = time.monotonic()
    await handler(route, FakeRequest("http://example.com/x", "document"))
    assert route.decision == "abort"
    assert time.monotonic() - started < 3.0, "the handler waited out the hanging pin"


@pytest.mark.real_sleeps
async def test_route_handler_resolve_is_bounded_by_the_dns_posture(monkeypatch):
    """A resolver that outlasts the DNS posture must not hold the route handler
    open (#405): the handler aborts fail-closed within the posture, not when the
    pin's own hang ends."""
    from app.transport import pinned_transport

    monkeypatch.setattr(pinned_transport, "DNS_WAIT_SECONDS", 0.2)
    handler = make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY, pin=_hanging_pin())
    await _handler_aborts_within_budget(handler)


@pytest.mark.real_sleeps
async def test_route_handler_resolve_honors_the_render_budget_parameter(monkeypatch):
    """The budget the browser already has (options.timeout_seconds) tightens the
    handler's resolve wait below the DNS posture."""
    handler = make_ssrf_route_handler(
        policy=UNRESTRICTED_HOST_POLICY, pin=_hanging_pin(), timeout_seconds=0.2
    )
    await _handler_aborts_within_budget(handler)


@pytest.mark.real_sleeps
async def test_websocket_handler_resolve_is_bounded_by_the_dns_posture(monkeypatch):
    """The websocket guard's resolve carries the same bound: a hung resolver
    closes the socket within the posture instead of stalling the upgrade."""
    from app.transport import pinned_transport
    from app.transport.browser_targets import make_ssrf_websocket_handler

    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    monkeypatch.setattr(pinned_transport, "DNS_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(socket, "getaddrinfo", _hanging_resolver(7.0))

    class FakeWS:
        url = "wss://example.test/socket"
        closed = False
        connected = False

        async def close(self):
            self.closed = True

        def connect_to_server(self):
            self.connected = True

    ws = FakeWS()
    handler = make_ssrf_websocket_handler(policy=UNRESTRICTED_HOST_POLICY)
    started = time.monotonic()
    await handler(ws)
    assert ws.closed and not ws.connected
    assert time.monotonic() - started < 3.0, "the websocket guard waited out the resolver"


@pytest.mark.real_sleeps
async def test_resolver_budget_timeout_is_a_resolution_failure_not_a_navigation_timeout(monkeypatch):
    """#405: when the bounded resolve outlasts the render budget, the caller gets
    the classifiable resolution failure (dns_error), not navigation_timeout."""
    from app.models.render_options import RenderOptions

    block_private_targets(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _hanging_resolver(7.0))
    page = BrowserFakePage()
    renderer = build_pinned_renderer(page)

    result = await renderer.render(
        canonical("http://example.com/"),
        RenderOptions(timeout_seconds=0.2, max_retries=0),
        policy=UNRESTRICTED_HOST_POLICY,
    )

    assert result.classification == "dns_error"
    assert result.error == "dns_error"
