"""M9: caller time budgets must be honored and bounded.

- RenderOptions.timeout_seconds was never enforced (page.goto ran on
  Playwright's default); the navigation must honor it and surface
  navigation_timeout through the existing retry envelope.
- The action-chain "wait" timeout was caller-unbounded (contrast the
  clamped wait_seconds); it is clamped to _MAX_ACTION_WAIT_SECONDS.
"""

import asyncio

from _browser_doubles import BrowserFakePage, build_pinned_renderer
from _policy_factories import UNRESTRICTED_HOST_POLICY

import app.crawl.strategies.browser as browser_mod
from app.crawl.strategies.browser import _MAX_ACTION_WAIT_SECONDS, BrowserRenderer
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
        # A navigation that never resolves. Sleeping five real seconds was how
        # this used to be "slow"; an Event nothing ever sets is slower than any
        # budget the render could be given, so the ONLY thing that can end the
        # wait is the navigation deadline the options carry.
        await asyncio.Event().wait()


class TestNavigationTimeout:
    async def test_navigation_honors_timeout_seconds(self, monkeypatch):
        page = _HangingPage()
        renderer = build_pinned_renderer(monkeypatch, page)
        monkeypatch.setattr(
            browser_mod,
            "_build_pinned_request",
            lambda url, headers, *, policy: ("http://1.2.3.4:80/", {"Host": "example.com"}),
        )
        # The deadline is enforced with asyncio.wait_for, which is loop-time
        # based, so the suite's zero-second sleep patch does not weaken it. The
        # caller's own guard is what makes a deadline that stopped firing a
        # FAILURE rather than a hang: a goto that never returns would
        # otherwise wedge the run instead of reporting.
        options = RenderOptions(timeout_seconds=0.05, max_retries=0)
        result = await asyncio.wait_for(
            renderer.render(
                "http://example.com/",
                options,
                policy=UNRESTRICTED_HOST_POLICY,
            ),
            timeout=5.0,
        )
        assert result.classification == "navigation_timeout"
