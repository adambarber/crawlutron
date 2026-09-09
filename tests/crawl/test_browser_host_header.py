"""M2: the pinned Host header must not leak to later page requests.

set_extra_http_headers() applies page-wide: without clearing after the
pinned navigation, every later request from that page (third-party XHR,
subsequent navigations) carries the spoofed Host. The render must set it
for the navigation, then clear it.
"""

import app.crawl.strategies.browser as browser_mod
from _testkit import BrowserFakePage, build_pinned_renderer
from app.models import RenderOptions


async def test_pinned_host_cleared_after_navigation(monkeypatch):
    page = BrowserFakePage()
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/", {"Host": "example.com"}),
    )
    result = await renderer._render_with_browser("http://example.com/", RenderOptions(browser_profile={}))
    assert result.status == "success"
    # Set for the pinned navigation, then cleared so later requests on the
    # page resolve Host from their own URLs again.
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}, {}]
