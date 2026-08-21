import urllib.request

import pytest

import app.crawl.strategies.browser as browser_mod
import app.crawl.strategies.document as doc_mod
import app.crawl.strategies.http as http_mod
from app.crawl.strategies.browser import BrowserRenderer, _resolve_browser_target, _assert_safe_final_url, _is_ssrf_blocked, _make_ssrf_route_handler
from app.models import RenderOptions


# --------------------------------------------------------------------------
# document.py: SSRF gate on urlopen (F2)
# --------------------------------------------------------------------------

async def test_document_extractor_blocks_internal_url(monkeypatch):
    # F2: the unprotected urllib urlopen must not be pointed at loopback.
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        doc_mod._default_document_text_extractor("http://127.0.0.1:9/secret.pdf")


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
    doc_mod._default_document_text_extractor("http://example.com/x.pdf")
    assert seen.get("url") == "http://example.com/x.pdf"


# --------------------------------------------------------------------------
# browser.py helpers (F2): pinning + post-navigation re-validation
# --------------------------------------------------------------------------

async def test_resolve_browser_target_pins_ip_and_preserves_host(monkeypatch):
    monkeypatch.setattr(
        browser_mod, "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/path", {"Host": "example.com"}),
    )
    pinned_url, headers = _resolve_browser_target("http://example.com/path")
    assert pinned_url == "http://1.2.3.4:80/path"
    assert headers == {"Host": "example.com"}


async def test_assert_safe_final_url_blocks_internal(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        _assert_safe_final_url("http://127.0.0.1:9/")
    # A public IP literal is allowed.
    _assert_safe_final_url("http://1.2.3.4/")


# --------------------------------------------------------------------------
# browser.py integration: _render_with_browser navigates to the pinned IP and
# re-validates the post-navigation URL (F2).
# --------------------------------------------------------------------------

class _FakePage:
    def __init__(self):
        self.goto_calls = []
        self.route_calls = []
        self.url = "http://1.2.3.4/"

    def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    def on(self, event, handler):
        pass

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    async def evaluate(self, *a, **k):
        return ""

    async def close(self):
        pass

    async def screenshot(self, **k):
        return b""

    async def query_selector(self, *a, **k):
        return None


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self, **kwargs):
        return self._page


async def _build_renderer(monkeypatch, page):
    renderer = BrowserRenderer(warmup_urls=[])

    async def fake_acquire(self, options):
        return _FakeBrowser(page)

    monkeypatch.setattr(BrowserRenderer, "_acquire_browser", fake_acquire)
    monkeypatch.setattr(BrowserRenderer, "_get_page_kwargs", lambda self, prof: {})
    monkeypatch.setattr(BrowserRenderer, "_wait_for_ready_state", _noop)
    monkeypatch.setattr(BrowserRenderer, "_run_action_chain", _noop)
    monkeypatch.setattr(BrowserRenderer, "_lazy_load_scroll", _noop)
    monkeypatch.setattr(BrowserRenderer, "_flatten_shadow_dom", _noop)
    monkeypatch.setattr(BrowserRenderer, "_auto_accept_cookie_consent", _noop)
    monkeypatch.setattr(BrowserRenderer, "_safe_close_page", _noop)
    monkeypatch.setattr(
        BrowserRenderer, "_extract_with_challenge_wait",
        lambda self, page, options: (_return(("<html>hi</html>", "hi", "ok", False))),
    )
    monkeypatch.setattr(BrowserRenderer, "_looks_like_pdf", lambda self, url: False)
    monkeypatch.setattr(BrowserRenderer, "_status_for_classification", lambda self, c: "success")
    return renderer


async def _noop(self, *a, **k):
    return None


def _return(value):
    async def coro():
        return value
    return coro()


async def test_render_navigates_to_pinned_ip_with_host_header(monkeypatch):
    page = _FakePage()
    renderer = await _build_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod, "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
    result = await renderer._render_with_browser("http://example.com/", RenderOptions(browser_profile={}))
    # Navigate to the pinned IP, not the hostname (DNS-rebinding safe).
    assert page.goto_calls[0][0] == "http://1.2.3.4:80/"
    assert page.goto_calls[0][1]["extra_http_headers"] == {"Host": "example.com"}
    assert result.status == "success"


async def test_render_rejects_internal_redirect_target(monkeypatch):
    page = _FakePage()
    page.url = "http://127.0.0.1:9/"  # site redirected the browser to loopback
    renderer = await _build_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod, "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        await renderer._render_with_browser("http://example.com/", RenderOptions(browser_profile={}))


# --------------------------------------------------------------------------
# F8: route-level guard aborts SSRF-unsafe requests (subresources + redirects)
# --------------------------------------------------------------------------

async def test_is_ssrf_blocked_classifies_targets(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    assert _is_ssrf_blocked("http://127.0.0.1:9/") is True
    assert _is_ssrf_blocked("http://169.254.169.254/latest/") is True
    assert _is_ssrf_blocked("http://1.2.3.4/") is False


async def test_ssrf_route_handler_aborts_unsafe_and_continues_safe(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    handler = _make_ssrf_route_handler()

    class _Route:
        def __init__(self):
            self.aborted = False
            self.continued = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    class _Req:
        def __init__(self, url):
            self.url = url

    bad = _Route()
    await handler(bad, _Req("http://127.0.0.1:9/"))
    assert bad.aborted and not bad.continued

    good = _Route()
    await handler(good, _Req("http://1.2.3.4/"))
    assert good.continued and not good.aborted


async def test_render_registers_ssrf_route_guard(monkeypatch):
    page = _FakePage()
    renderer = await _build_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod, "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
    await renderer._render_with_browser("http://example.com/", RenderOptions(browser_profile={}))
    # The SSRF route guard must be registered on the page.
    assert any(pattern == "**/*" for pattern, _ in page.route_calls)
