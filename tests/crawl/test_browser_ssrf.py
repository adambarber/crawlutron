import urllib.request

import pytest
from _browser_doubles import BrowserFakePage, FakeRequest, FakeRoute, build_pinned_renderer, pinned_renderer
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
        doc_mod._default_document_text_extractor(
            "http://127.0.0.1:9/secret.pdf", UNRESTRICTED_HOST_POLICY
        )


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
        "_build_pinned_request",
        lambda url, headers, *, policy: ("http://1.2.3.4:80/path", {"Host": "example.com"}),
    )
    pinned_url, headers = _resolve_browser_target(
        "http://example.com/path", policy=UNRESTRICTED_HOST_POLICY
    )
    assert pinned_url == "http://1.2.3.4:80/path"
    assert headers == {"Host": "example.com"}


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
    page, renderer = pinned_renderer(monkeypatch)
    result = await renderer._render_with_browser(
            "http://example.com/",
            RenderOptions(browser_profile={}),
            policy=UNRESTRICTED_HOST_POLICY,
        )
    # Navigate to the pinned IP, not the hostname (DNS-rebinding safe).
    assert page.goto_calls[0][0] == "http://1.2.3.4:80/"
    # Should use set_extra_http_headers, NOT extra_http_headers param;
    # the pinned Host is cleared right after navigation (M2).
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}, {}]
    assert result.status == "success"


async def test_render_rejects_internal_redirect_target(monkeypatch):
    page = BrowserFakePage()
    page.url = "http://127.0.0.1:9/"  # site redirected the browser to loopback
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers, *, policy: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
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
    assert _is_ssrf_blocked(
        "http://169.254.169.254/latest/", policy=UNRESTRICTED_HOST_POLICY
    ) is True
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
    page = BrowserFakePage()
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers, *, policy: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
    await renderer._render_with_browser(
            "http://example.com/",
            RenderOptions(browser_profile={}),
            policy=UNRESTRICTED_HOST_POLICY,
        )
    # The SSRF route guard must be registered on the page.
    assert any(pattern == "**/*" for pattern, _ in page.route_calls)
