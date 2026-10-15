import socket
import threading

import pytest

import app.transport.document as doc_mod
import app.transport.pinned_transport as http_mod
from app.transport.browser import make_ssrf_route_handler, resolve_browser_target
from app.transport.pinned_transport import build_pinned_request, build_pinned_request_async
from tests._assertions import block_private_targets, host_resolves_to
from tests._browser_doubles import (
    FakeRequest,
    FakeRoute,
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

    The gate is the fetch walk's first pin: ``default_document_text_extractor``
    fetches through ``apinned_hops``, and the walk pins before anything
    connects. A scripted client keeps the test off the network while the
    recording gate proves the property: a hand-rolled loop that skipped the
    pin would reach the client with no gate entry, and a gate entry carrying a
    different URL would mean the fetch was not the one that was checked.

    The extractor's own contract swallows every fetch failure into empty
    text, so the gate entry -- not an escaping sentinel -- is the observable.
    """
    from unittest.mock import patch

    from tests._transport_doubles import MockResponse, scripted_transport_client

    seen = {}

    def fake_check(url):
        seen["url"] = url

    monkeypatch.setattr(http_mod, "check_ssrf_protection", fake_check)
    with (
        host_resolves_to(monkeypatch, "93.184.216.34"),
        patch(
            "app.transport.http_client_pool.get_async_http_client",
            return_value=scripted_transport_client(
                [MockResponse(200, {"content-type": "application/pdf"}, b"")]
            ),
        ),
    ):
        doc_mod.default_document_text_extractor(canonical("http://example.com/x.pdf"))
    assert seen.get("url") == canonical("http://example.com/x.pdf")


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
    thread stalls every other in-flight crawl in the process once per page -- the
    loop is shared, so the cost is paid by traffic this page has nothing to do with.

    The observer is ``socket.getaddrinfo`` rather than the pin builder, because the
    builder is what got fixed and the resolver is what is actually blocking; a test
    pinned to the thing that was corrected proves the correction and not the
    property. The address is canned, so this is deterministic and does no DNS.

    Private targets are blocked because the session-wide loopback hatch is the
    other way this test would prove nothing: with it on, ``resolve_and_pin_host``
    returns the hostname before it resolves, the observer never fires, and the
    assertion ends up checking that nothing happened.
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
