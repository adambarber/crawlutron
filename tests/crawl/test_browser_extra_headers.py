import pytest

import app.crawl.strategies.browser as browser_mod
from app.crawl.strategies.browser import BrowserRenderer, _resolve_browser_target
from app.models import RenderOptions


class _StrictFakePage:
    """FakePage that mimics real Playwright API - rejects extra_http_headers."""
    def __init__(self):
        self.goto_calls = []
        self.route_calls = []
        self.set_extra_http_headers_calls = []
        self.url = "http://1.2.3.4/"

    def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    def on(self, event, handler):
        pass

    async def goto(self, url, wait_until="load", **kwargs):
        # Real Playwright goto does NOT accept extra_http_headers
        if "extra_http_headers" in kwargs:
            raise TypeError("unexpected keyword argument 'extra_http_headers'")
        self.goto_calls.append((url, kwargs))

    def set_extra_http_headers(self, headers):
        self.set_extra_http_headers_calls.append(headers)

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


async def test_render_navigates_to_pinned_ip_with_host_header_uses_correct_api(monkeypatch):
    """Test that the browser uses set_extra_http_headers instead of extra_http_headers param (F2)."""
    page = _StrictFakePage()
    renderer = await _build_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod, "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
    result = await renderer._render_with_browser("http://example.com/", RenderOptions(browser_profile={}))
    # Navigate to the pinned IP, not the hostname (DNS-rebinding safe).
    assert page.goto_calls[0][0] == "http://1.2.3.4:80/"
    # Should use set_extra_http_headers, NOT extra_http_headers param
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}]
    assert result.status == "success"
