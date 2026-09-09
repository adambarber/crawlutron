"""M2: the pinned Host header must not leak to later page requests.

set_extra_http_headers() applies page-wide: without clearing after the
pinned navigation, every later request from that page (third-party XHR,
subsequent navigations) carries the spoofed Host. The render must set it
for the navigation, then clear it.
"""

from _testkit import BrowserFakePage, build_pinned_renderer

import app.crawl.strategies.browser as browser_mod
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


_MISSING = object()


async def test_route_continue_omits_empty_headers(monkeypatch):
    # M1: an explicit headers={} can replace (wipe) the request's headers
    # in Playwright; the kwarg must be omitted when there are no overrides.
    handler = browser_mod._make_ssrf_route_handler()
    seen = {}

    class _Route:
        async def continue_(self, url=None, headers=_MISSING):
            seen["url"] = url
            seen["headers"] = headers

        async def abort(self):
            seen["aborted"] = True

    class _Request:
        url = "https://example.com/app.js"
        resource_type = "script"

    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers: (url, {}),  # https: URL kept, no Host override
    )
    await handler(_Route(), _Request())
    assert "aborted" not in seen
    assert seen["headers"] is _MISSING


async def test_route_continue_passes_host_override(monkeypatch):
    # The http pinned-IP hop still carries its Host override.
    handler = browser_mod._make_ssrf_route_handler()
    seen = {}

    class _Route:
        async def continue_(self, url=None, headers=_MISSING):
            seen["url"] = url
            seen["headers"] = headers

        async def abort(self):
            seen["aborted"] = True

    class _Request:
        url = "http://example.com/app.js"
        resource_type = "script"

    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers: ("http://1.2.3.4:80/app.js", {"Host": "example.com"}),
    )
    await handler(_Route(), _Request())
    assert "aborted" not in seen
    assert seen["url"] == "http://1.2.3.4:80/app.js"
    assert seen["headers"] == {"Host": "example.com"}
