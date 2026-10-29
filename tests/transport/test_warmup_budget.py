"""#532: the warmup page.goto must carry a render-phase budget.

Every other navigation on the render path is budgeted through the
render_phase_seconds vocabulary (#482); the warmup goto inherited Playwright's
default (30s or indefinite), letting a slow-warmup origin hold the render
worker's navigation slot unbounded.
"""

from __future__ import annotations

from crawlutron.transport.browser_render_phases import warmup_pages
from crawlutron.transport.timeouts import render_phase_seconds
from tests._browser_doubles import BrowserFakePage
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


class _GotoKwargsPage(BrowserFakePage):
    """Records every goto's kwargs so the budget is asserted, not guessed."""

    def __init__(self):
        super().__init__()
        self.goto_kwargs_seen: list[dict] = []

    async def goto(self, url, wait_until="load", **kwargs):
        await super().goto(url, wait_until=wait_until, **kwargs)
        self.goto_kwargs_seen.append(kwargs)


async def test_warmup_goto_carries_the_render_phase_budget() -> None:
    """The warmup navigation receives timeout=render_phase_seconds(budget), not
    Playwright's unbounded default (#532)."""
    page = _GotoKwargsPage()
    await warmup_pages(
        page,
        wait_for_ready=_no_wait,
        warmup_urls=["http://example.com/warm"],
        policy=UNRESTRICTED_HOST_POLICY,
        timeout_seconds=7.5,
    )
    assert page.goto_kwargs_seen == [{"timeout": render_phase_seconds(7.5)}], page.goto_kwargs_seen


async def test_warmup_goto_budget_is_floored_like_every_render_phase() -> None:
    """The same vocabulary floors a sub-second budget at 1s -- the warmup must
    not re-derive a different floor (#482 single-sourcing)."""
    page = _GotoKwargsPage()
    await warmup_pages(
        page,
        wait_for_ready=_no_wait,
        warmup_urls=["http://example.com/warm"],
        policy=UNRESTRICTED_HOST_POLICY,
        timeout_seconds=0.2,
    )
    assert page.goto_kwargs_seen == [{"timeout": render_phase_seconds(0.2)}], page.goto_kwargs_seen
    assert page.goto_kwargs_seen[0]["timeout"] >= 1.0


async def test_warmup_goto_budget_reaches_every_warmup_url() -> None:
    """Each warmup URL's navigation is budgeted, not just the first."""
    page = _GotoKwargsPage()
    await warmup_pages(
        page,
        wait_for_ready=_no_wait,
        warmup_urls=["http://a.example/", "http://b.example/"],
        policy=UNRESTRICTED_HOST_POLICY,
        timeout_seconds=3.0,
    )
    assert [k.get("timeout") for k in page.goto_kwargs_seen] == [3.0, 3.0]


async def _no_wait(_page):
    return None
