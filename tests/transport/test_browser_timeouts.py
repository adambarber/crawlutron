"""Caller time budgets must be honored and bounded.

The navigation must honor ``timeout_seconds`` and surface ``navigation_timeout``;
the action-chain ``wait`` was caller-unbounded and is clamped.
"""

import asyncio

import pytest
from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.render_options import RenderOptions
from crawlutron.transport.browser_driver import MAX_ACTION_WAIT_SECONDS, BrowserDriver
from tests._browser_doubles import BrowserFakePage, pinned_page_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


class _WaitUntilRecordingPage(BrowserFakePage):
    """Records the ``wait_until`` each goto carries, so the navigation settle
    strategy is asserted rather than inferred."""

    def __init__(self):
        super().__init__()
        self.wait_untils = []

    async def goto(self, url, wait_until="load", **kwargs):
        self.wait_untils.append(wait_until)
        await super().goto(url, wait_until=wait_until, **kwargs)


class TestNavigationNetworkSettle:
    async def test_navigation_waits_for_network_idle(self, monkeypatch):
        page, renderer = pinned_page_renderer(monkeypatch, _WaitUntilRecordingPage())
        await renderer.render(
            NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY),
            RenderOptions(timeout_seconds=1, max_retries=0),
            policy=UNRESTRICTED_HOST_POLICY,
        )
        assert "networkidle" in page.wait_untils, page.wait_untils


class _TimeoutRecordingPage(BrowserFakePage):
    def __init__(self):
        super().__init__()
        self.wait_timeouts = []

    async def wait_for_selector(self, selector, timeout=None, **kwargs):
        self.wait_timeouts.append(timeout)
        return None


class TestWaitActionClamp:
    @pytest.mark.parametrize(
        ("timeout", "assert_timeout"),
        [
            pytest.param(
                890000,
                lambda seen: seen == [int(MAX_ACTION_WAIT_SECONDS * 1000)],
                id="caller-value-is-clamped",
            ),
            pytest.param(
                0,
                # Playwright treats timeout=0 as "no timeout", so the floor is what keeps the wait bounded
                # rather than the caller's value.
                lambda seen: len(seen) == 1 and seen[0] >= 1,
                id="zero-is-floored-not-disabling",
            ),
            pytest.param(
                "forever",
                lambda seen: seen == [5000],
                id="garbage-falls-back-to-default",
            ),
        ],
    )
    async def test_wait_timeout_is_bounded(self, timeout, assert_timeout):
        page = _TimeoutRecordingPage()
        await BrowserDriver().run_action_chain(page, [{"type": "wait", "selector": "#x", "timeout": timeout}])
        assert_timeout(page.wait_timeouts)


class _HangingPage(BrowserFakePage):
    async def goto(self, url, wait_until="load", **kwargs):
        self.goto_calls.append((url, kwargs))
        # A navigation that never resolves.
        await asyncio.Event().wait()


class TestNavigationTimeout:
    async def test_navigation_honors_timeout_seconds(self, monkeypatch):
        page, renderer = pinned_page_renderer(monkeypatch, _HangingPage())
        # The deadline is enforced with asyncio.wait_for, which is loop-time based, so the suite's zero-second
        # sleep patch does not weaken it.
        options = RenderOptions(timeout_seconds=1, max_retries=0)
        result = await asyncio.wait_for(
            renderer.render(
                NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY),
                options,
                policy=UNRESTRICTED_HOST_POLICY,
            ),
            timeout=5.0,
        )
        assert result.classification == "navigation_timeout"
