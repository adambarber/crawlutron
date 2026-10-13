"""The pinned Host header must not leak to later page requests.

set_extra_http_headers() applies page-wide: without clearing after the
pinned navigation, every later request from that page (third-party XHR,
subsequent navigations) carries the spoofed Host. The render must set it
for the navigation, then clear it.
"""

from tests._browser_doubles import ROUTE_ARG_UNSET, render_pinned, run_route_with_pinner


async def test_pinned_host_cleared_after_navigation(monkeypatch):
    page, result = await render_pinned(monkeypatch)
    assert result.status == "success"
    # Set for the pinned navigation, then cleared so later requests on the page resolve Host from their own
    # URLs again.
    assert page.set_extra_http_headers_calls == [{"Host": "example.com"}, {}]


async def test_route_continue_omits_empty_headers(monkeypatch):
    # An explicit headers={} can replace (wipe) the request's headers in Playwright; the kwarg must be omitted
    # when there are no overrides.
    route = await run_route_with_pinner("https://example.com/app.js", resource_type="script")
    assert route.decision == "continue"
    assert route.continued_headers is ROUTE_ARG_UNSET


async def test_route_continue_passes_host_override(monkeypatch):
    # The http pinned-IP hop still carries its Host override.
    route = await run_route_with_pinner(
        "http://example.com/app.js",
        pinned=("http://1.2.3.4:80/app.js", {"Host": "example.com"}),
        resource_type="script",
    )
    assert route.decision == "continue"
    assert route.continued_url == "http://1.2.3.4:80/app.js"
    assert route.continued_headers == {"Host": "example.com"}
