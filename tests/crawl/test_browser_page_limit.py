import pytest

pytestmark = pytest.mark.browser

"""Throughput regression test: concurrent browser renders are capped.

Each render opens a browser tab; without a cap a burst of N requests opens N
tabs and crashes/OOMs the shared Camoufox process.
"""


import asyncio

from app.crawl.strategies.browser import BrowserRenderer
from app.models import RenderOptions



class _FakeElement:
    def __init__(self, selector):
        self.selector = selector

class _FakePage:
    def __init__(self, tracker):
        self._tracker = tracker
        self.url = "http://example.com/"

    async def route(self, pattern, handler):
        return None

    def on(self, event, handler):
        return None

    async def set_extra_http_headers(self, headers):
        return None

    async def goto(self, url, wait_until="load", **kwargs):
        # Simulate render work so overlapping renders are observable.
        self._tracker["n"] += 1
        self._tracker["max"] = max(self._tracker["max"], self._tracker["n"])
        await asyncio.sleep(0.05)
        self._tracker["n"] -= 1

    async def evaluate(self, expression):
        if "readyState" in expression:
            return "complete"
        if "document.title" in expression:
            return "Fake Title"
        return ""

    async def content(self):
        return "<html><body><h1>Visible body text</h1></body></html>"

    async def close(self):
        return None

class _FakeBrowser:
    def __init__(self, tracker):
        self._tracker = tracker

    async def new_page(self, **kwargs):
        return _FakePage(self._tracker)

    @property
    def is_closed(self):
        return False

class _FakeBrowserCM:
    def __init__(self, tracker):
        self.browser = _FakeBrowser(tracker)

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, *exc):
        return False

async def test_concurrent_page_renders_capped(monkeypatch, real_sleeps):
    monkeypatch.setenv("BROWSER_CRAWLER_MAX_CONCURRENT_PAGES", "2")
    tracker = {"n": 0, "max": 0}
    renderer = BrowserRenderer(browser_factory=lambda **kw: _FakeBrowserCM(tracker))
    try:
        opts = RenderOptions(browser_profile={})
        await asyncio.gather(
            *[renderer.render("http://example.com/", opts) for _ in range(6)]
        )
    finally:
        await renderer.close()
    assert tracker["max"] <= 2, f"page cap ignored: {tracker}"
    assert tracker["max"] >= 2, f"cap serialized renders entirely: {tracker}"

