import socket
import threading
import urllib.request

import pytest
from _assertions import block_private_targets
from _browser_doubles import FakeRequest, FakeRoute, pinned_page_renderer, render_pinned, stub_pinned_request
from _policy_factories import UNRESTRICTED_HOST_POLICY

import app.crawl.strategies.browser as browser_mod
import app.crawl.strategies.document as doc_mod
import app.crawl.strategies.pinned_transport as http_mod
from app.crawl.strategies.browser import (
    _assert_safe_final_url,
    _is_ssrf_blocked,
    _make_ssrf_route_handler,
    _resolve_browser_target,
)
from app.models import RenderOptions

# --------------------------------------------------------------------------
# document.py: SSRF gate on urlopen (F2)
# --------------------------------------------------------------------------


async def test_document_extractor_blocks_internal_url(monkeypatch):
    # F2: the unprotected urllib urlopen must not be pointed at loopback.
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        doc_mod._default_document_text_extractor("http://127.0.0.1:9/secret.pdf", UNRESTRICTED_HOST_POLICY)


async def test_document_extractor_invokes_ssrf_check(monkeypatch):
    seen = {}

    def fake_check(url):
        seen["url"] = url

    monkeypatch.setattr(http_mod, "_check_ssrf_protection", fake_check)

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"%PDF-1.4 fake"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp())

    # pdfplumber will fail to parse the fake bytes -> returns ""; we only care
    # that the SSRF gate was invoked with the requested URL.
    doc_mod._default_document_text_extractor("http://example.com/x.pdf", UNRESTRICTED_HOST_POLICY)
    assert seen.get("url") == "http://example.com/x.pdf"


# --------------------------------------------------------------------------
# browser.py helpers (F2): pinning + post-navigation re-validation
# --------------------------------------------------------------------------


async def test_resolve_browser_target_pins_ip_and_preserves_host(monkeypatch):
    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request_async",
        stub_pinned_request(("http://1.2.3.4:80/path", {"Host": "example.com"})),
    )
    pinned_url, headers = await _resolve_browser_target("http://example.com/path", policy=UNRESTRICTED_HOST_POLICY)
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
    other way this test would prove nothing: with it on, ``_resolve_and_pin_host``
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

    await _resolve_browser_target("http://example.com/path", policy=UNRESTRICTED_HOST_POLICY)

    assert resolving_threads, "no resolver call was observed, so the invariant was not exercised"
    assert all(ident != loop_thread for ident in resolving_threads), (
        "navigation pinning resolved DNS on the event loop thread, which blocks "
        f"every other crawl in the process once per rendered page (loop thread "
        f"{loop_thread}, resolver threads {resolving_threads})"
    )


async def test_assert_safe_final_url_blocks_internal(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        _assert_safe_final_url("http://127.0.0.1:9/", policy=UNRESTRICTED_HOST_POLICY)
    # A public IP literal is allowed.
    _assert_safe_final_url("http://1.2.3.4/", policy=UNRESTRICTED_HOST_POLICY)


# --------------------------------------------------------------------------
# browser.py integration: _render_with_browser navigates to the pinned IP and
# re-validates the post-navigation URL (F2).
# --------------------------------------------------------------------------


async def test_render_navigates_to_pinned_ip_with_host_header(monkeypatch):
    page, result = await render_pinned(monkeypatch)
    # Navigate to the pinned IP, not the hostname (DNS-rebinding safe).
    assert page.goto_calls[0][0] == "http://1.2.3.4:80/"
    # Should use set_extra_http_headers, NOT extra_http_headers param;
    # the pinned Host is cleared right after navigation (M2).
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}, {}]
    assert result.status == "success"


async def test_render_rejects_internal_redirect_target(monkeypatch):
    page, renderer = pinned_page_renderer(monkeypatch)
    page.url = "http://127.0.0.1:9/"  # site redirected the browser to loopback
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        await renderer._render_with_browser(
            "http://example.com/",
            RenderOptions(browser_profile={}),
            policy=UNRESTRICTED_HOST_POLICY,
        )


# --------------------------------------------------------------------------
# F8: route-level guard aborts SSRF-unsafe requests (subresources + redirects)
# --------------------------------------------------------------------------


async def test_is_ssrf_blocked_classifies_targets(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    assert _is_ssrf_blocked("http://127.0.0.1:9/", policy=UNRESTRICTED_HOST_POLICY) is True
    assert _is_ssrf_blocked("http://169.254.169.254/latest/", policy=UNRESTRICTED_HOST_POLICY) is True
    assert _is_ssrf_blocked("http://1.2.3.4/", policy=UNRESTRICTED_HOST_POLICY) is False


async def test_ssrf_route_handler_aborts_unsafe_and_continues_safe(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)

    bad = FakeRoute()
    await handler(bad, FakeRequest("http://127.0.0.1:9/", "document"))
    assert bad.decision == "abort"

    good = FakeRoute()
    await handler(good, FakeRequest("http://1.2.3.4/", "document"))
    assert good.decision == "continue"


async def test_render_registers_ssrf_route_guard(monkeypatch):
    page, renderer = pinned_page_renderer(monkeypatch)
    await renderer._render_with_browser(
        "http://example.com/",
        RenderOptions(browser_profile={}),
        policy=UNRESTRICTED_HOST_POLICY,
    )
    # The SSRF route guard must be registered on the page.
    assert any(pattern == "**/*" for pattern, _ in page.route_calls)
