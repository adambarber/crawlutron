"""An unresponsive page.evaluate must not wedge the pooled renderer.

Playwright's evaluate carries NO default deadline: a hostile script in an
infinite loop parks the call forever, holding the browser-pool slot and the
caller's crawl. Every other render op rides a bounded wait; evaluate is the
hole. The render must answer a wedged evaluate with a navigation_timeout
verdict and still release the page.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.models.render_options import RenderOptions
from tests._browser_doubles import (
    BrowserFakePage,
    FakeBrowserDriver,
    build_pinned_renderer,
    stub_pinned_request,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


class _WedgedEvaluatePage(BrowserFakePage):
    """A page whose evaluate never returns: the wedged-renderer stand-in."""

    async def evaluate(self, *a, **k):
        await asyncio.Event().wait()


class _CloseRecordingDriver(FakeBrowserDriver):
    """Counts page closes so the slot release is pinned, not assumed."""

    def __init__(self, page):
        super().__init__(page)
        self.closed_pages = 0

    async def safe_close_page(self, page):
        self.closed_pages += 1


async def test_wedged_evaluate_answers_as_navigation_timeout_and_releases_the_page():
    """A page.evaluate that never resolves must end the render bounded (the
    outer wait_for is the hang guard, never the mechanism -- it exceeds the
    5s evaluate ceiling), verdict navigation_timeout, page closed once."""
    page = _WedgedEvaluatePage()
    driver = _CloseRecordingDriver(page)
    renderer = build_pinned_renderer(page, driver=driver, pin=stub_pinned_request())
    # No-text page: extraction skips DOM reads, the PDF fallback and the
    # metadata title evaluate are the evaluate sites this budget must cover.
    try:
        result = await asyncio.wait_for(
            renderer.render(
                canonical("http://example.com/"),
                RenderOptions(include_html=False, include_text=False, timeout_seconds=1, max_retries=0),
                policy=UNRESTRICTED_HOST_POLICY,
            ),
            timeout=8.0,
        )
    except TimeoutError:
        pytest.fail(
            "the render outlived the 8s hang guard (evaluate ceiling is 5s): "
            "a wedged page.evaluate holds the pooled renderer unbounded"
        )
    assert result.classification == "navigation_timeout", result.classification
    assert driver.closed_pages == 1, "the wedged evaluate must not strand the pooled page"
