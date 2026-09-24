"""M2: the pinned Host header must not leak to later page requests.

set_extra_http_headers() applies page-wide: without clearing after the
pinned navigation, every later request from that page (third-party XHR,
subsequent navigations) carries the spoofed Host. The render must set it
for the navigation, then clear it.
"""

from _browser_doubles import ROUTE_ARG_UNSET, FakeRequest, FakeRoute, pinned_renderer
from _policy_factories import UNRESTRICTED_HOST_POLICY

import app.crawl.strategies.browser as browser_mod
from app.models import RenderOptions


async def test_pinned_host_cleared_after_navigation(monkeypatch):
    page, renderer = pinned_renderer(monkeypatch)
    result = await renderer._render_with_browser(
        "http://example.com/",
        RenderOptions(browser_profile={}),
        policy=UNRESTRICTED_HOST_POLICY,
    )
    assert result.status == "success"
    # Set for the pinned navigation, then cleared so later requests on the
    # page resolve Host from their own URLs again.
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}, {}]


async def test_route_continue_omits_empty_headers(monkeypatch):
    # M1: an explicit headers={} can replace (wipe) the request's headers
    # in Playwright; the kwarg must be omitted when there are no overrides.
    handler = browser_mod._make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    route = FakeRoute()
    request = FakeRequest("https://example.com/app.js", "script")

    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers, *, policy: (url, {}),  # https: URL kept, no Host override
    )
    await handler(route, request)
    assert route.decision == "continue"
    assert route.continued_headers is ROUTE_ARG_UNSET


async def test_route_continue_passes_host_override(monkeypatch):
    # The http pinned-IP hop still carries its Host override.
    handler = browser_mod._make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    route = FakeRoute()
    request = FakeRequest("http://example.com/app.js", "script")

    monkeypatch.setattr(
        browser_mod,
        "_build_pinned_request",
        lambda url, headers, *, policy: ("http://1.2.3.4:80/app.js", {"Host": "example.com"}),
    )
    await handler(route, request)
    assert route.decision == "continue"
    assert route.continued_url == "http://1.2.3.4:80/app.js"
    assert route.continued_headers == {"Host": "example.com"}
