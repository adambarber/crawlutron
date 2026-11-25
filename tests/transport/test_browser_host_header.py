"""S4: browser routes through the loopback egress proxy; no Host-header
pinning. The browser continues with logical URLs and must not set page-wide
Host overrides.
"""

from tests._browser_doubles import ROUTE_ARG_UNSET, run_route_with_pinner


async def test_route_continue_omits_empty_headers(monkeypatch):
    # No Host override in proxy mode; headers kwarg must be omitted so Playwright
    # does not wipe UA/cookies/accept.
    route = await run_route_with_pinner("https://example.com/app.js", resource_type="script")
    assert route.decision == "continue"
    assert route.continued_headers is ROUTE_ARG_UNSET


async def test_route_continue_uses_logical_url_no_host_override(monkeypatch):
    # S4: proxy owns dialing; browser continues with logical URL, no Host header.
    route = await run_route_with_pinner(
        "http://example.com/app.js",
        pinned=("http://1.2.3.4:80/app.js", {"Host": "example.com"}),
        resource_type="script",
    )
    assert route.decision == "continue"
    assert route.continued_url == "http://example.com/app.js"
    assert route.continued_headers is ROUTE_ARG_UNSET
