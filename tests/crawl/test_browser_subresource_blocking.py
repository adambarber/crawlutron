import pytest

pytestmark = pytest.mark.browser

"""Browser route handler blocks tracking subresources and (in reduced mode)
non-essential subresources to cut render bandwidth (#2)."""


import asyncio
from unittest.mock import patch

import app.crawl.strategies.browser as browser_mod
from app.crawl.strategies.browser import _make_ssrf_route_handler



class _FakeRoute:
    def __init__(self):
        self.actions = []

    async def abort(self):
        self.actions.append(("abort",))

    async def continue_(self, **kw):
        self.actions.append(("continue", kw))

    def decision(self):
        return self.actions[-1][0] if self.actions else None

class _FakeRequest:
    def __init__(self, url, resource_type="document"):
        self.url = url
        self.resource_type = resource_type

def _handler_for(resource_mode=None, *, include_screenshot=False):
    return _make_ssrf_route_handler(resource_mode, include_screenshot=include_screenshot)

async def test_analytics_aborted_regardless_of_mode():
    route = _FakeRoute()
    handler = _handler_for(None)
    await handler(route, _FakeRequest("https://www.google-analytics.com/analytics.js", "script"))
    assert route.decision() == "abort"

async def test_pinterest_aborted_with_and_without_www():
    for url in ("https://pinterest.com/pin/123", "https://www.pinterest.com/pin/123"):
        route = _FakeRoute()
        await _handler_for(None)(route, _FakeRequest(url, "document"))
        assert route.decision() == "abort", url

async def test_cdcgov_aborted_with_and_without_www():
    # suffix match covers apex and subdomains alike
    for url in ("https://cdc.gov/foo", "https://www.cdc.gov/bar", "https://data.cdc.gov/baz"):
        route = _FakeRoute()
        await _handler_for(None)(route, _FakeRequest(url, "document"))
        assert route.decision() == "abort", url

async def test_xcom_aborted_regardless_of_mode():
    # x.com (X/Twitter) is on the hard-skip tracking list: aborted in every mode,
    # including bare-apex and www subdomain forms.
    for url in ("https://x.com/i/web/status/1", "https://www.x.com/home"):
        route = _FakeRoute()
        await _handler_for(None)(route, _FakeRequest(url, "document"))
        assert route.decision() == "abort", url

async def test_image_blocked_by_default_unless_screenshot():
    # default (no screenshot requested) -> abort unconditionally (#4)
    route = _FakeRoute()
    await _handler_for(None)(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "abort"

    # reduced mode -> also abort
    route = _FakeRoute()
    await _handler_for("reduced")(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "abort"

    # screenshot explicitly requested -> continue (renders need images/fonts)
    route = _FakeRoute()
    with patch.object(browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {})):
        await _handler_for(None, include_screenshot=True)(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "continue"

async def test_document_always_continues_with_pinned_url():
    route = _FakeRoute()
    with patch.object(browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {"Host": "x.test"})):
        await _handler_for(None)(route, _FakeRequest("https://x.test/page", "document"))
    assert route.decision() == "continue"
    assert route.actions[-1][1]["url"] == "http://1.2.3.4/"

