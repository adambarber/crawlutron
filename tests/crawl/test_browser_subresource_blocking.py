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


def _handler_for(resource_mode):
    return _make_ssrf_route_handler(resource_mode)


async def test_analytics_aborted_regardless_of_mode():
    route = _FakeRoute()
    handler = _handler_for(None)
    await handler(route, _FakeRequest("https://www.google-analytics.com/analytics.js", "script"))
    assert route.decision() == "abort"


async def test_image_blocked_only_in_reduced_mode():
    # reduced mode -> abort
    route = _FakeRoute()
    await _handler_for("reduced")(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "abort"

    # full mode -> not aborted (still goes through SSRF + continue)
    route = _FakeRoute()
    with patch.object(browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {})):
        await _handler_for(None)(route, _FakeRequest("https://x.test/logo.png", "image"))
    assert route.decision() == "continue"


async def test_document_always_continues_with_pinned_url():
    route = _FakeRoute()
    with patch.object(browser_mod, "_build_pinned_request", return_value=("http://1.2.3.4/", {"Host": "x.test"})):
        await _handler_for(None)(route, _FakeRequest("https://x.test/page", "document"))
    assert route.decision() == "continue"
    assert route.actions[-1][1]["url"] == "http://1.2.3.4/"
