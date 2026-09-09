"""M9: caller time budgets must be honored and bounded.

- RenderOptions.timeout_seconds was never enforced (page.goto ran on
  Playwright's default); the navigation must honor it and surface
  navigation_timeout through the existing retry envelope.
- The action-chain "wait" timeout was caller-unbounded (contrast the
  clamped wait_seconds); it is clamped to _MAX_ACTION_WAIT_SECONDS.
"""

import asyncio

import app.crawl.strategies.browser as browser_mod
from _testkit import BrowserFakePage, build_pinned_renderer
from app.crawl.strategies.browser import BrowserRenderer, _MAX_ACTION_WAIT_SECONDS
from app.models import RenderOptions


class _TimeoutRecordingPage(BrowserFakePage):
    def __init__(self):
        super().__init__()
        self.wait_timeouts = []

    async def wait_for_selector(self, selector, timeout=None):
        self.wait_timeouts.append(timeout)
        return None


class TestWaitActionClamp:
    async def test_wait_timeout_clamped_to_max(self):
        page = _TimeoutRecordingPage()
        renderer = BrowserRenderer()
        await renderer._run_action_chain(page, [{"type": "wait", "selector": "#x", "timeout": 890000}])
        assert page.wait_timeouts == [int(_MAX_ACTION_WAIT_SECONDS * 1000)]

    async def test_wait_timeout_zero_not_disabling(self):
        # Playwright treats timeout=0 as "no timeout": floor to stay bounded.
        page = _TimeoutRecordingPage()
        renderer = BrowserRenderer()
        await renderer._run_action_chain(page, [{"type": "wait", "selector": "#x", "timeout": 0}])
        assert page.wait_timeouts[0] >= 1

    async def test_wait_timeout_garbage_falls_back_to_default(self):
        page = _TimeoutRecordingPage()
        renderer = BrowserRenderer()
        await renderer._run_action_chain(page, [{"type": "wait", "selector": "#x", "timeout": "forever"}])
        assert page.wait_timeouts == [5000]


class _HangingPage(BrowserFakePage):
    async def goto(self, url, wait_until="load", **kwargs):
        self.goto_calls.append((url, kwargs))
        await asyncio.sleep(5)


class TestNavigationTimeout:
    async def test_navigation_honors_timeout_seconds(self, monkeypatch, real_sleeps):
        page = _HangingPage()
        renderer = build_pinned_renderer(monkeypatch, page)
        monkeypatch.setattr(
            browser_mod,
            "_build_pinned_request",
            lambda url, headers: ("http://1.2.3.4:80/", {"Host": "example.com"}),
        )
        options = RenderOptions(timeout_seconds=1, max_retries=0)
        result = await renderer.render("http://example.com/", options)
        assert result.classification == "navigation_timeout"
