"""Every render-path page read carries the evaluate ceiling -- no bare holes.

Round 2 of the wedged-renderer law: content(), shadow flattening, readyState
polls, lazy-scroll probes, and the consent fallback were still unbounded; a
never-returning await in any of them parks the pooled page past every bound.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.render_options import RenderOptions
from crawlutron.transport import driver_pages
from crawlutron.transport import timeouts as transport_timeouts
from crawlutron.transport.browser import BrowserDriver
from tests._browser_doubles import (
    BrowserFakePage,
    FakeBrowserDriver,
    build_pinned_renderer,
    stub_pinned_request,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

_HANG_GUARD_SECONDS = 8.0


@pytest.fixture(autouse=True)
def _injected_ceiling(monkeypatch):
    """Bound page reads at 0.1s: the wedges cancel by the seam, not by
    waiting out the real 5s ceiling (30s of battery tail removed)."""
    monkeypatch.setattr(transport_timeouts, "EVALUATE_CEILING_SECONDS_OVERRIDE", 0.1)


class _WedgedReadPage(BrowserFakePage):
    """A page whose named read never returns; every other read answers."""

    def __init__(self, wedge: str) -> None:
        super().__init__()
        self._wedge = wedge

    async def evaluate(self, expression, *a, **k):
        if self._wedge in str(expression):
            await asyncio.Event().wait()
        return ""

    async def content(self):
        if self._wedge == "content":
            await asyncio.Event().wait()
        return "<html></html>"


class _CloseRecordingDriver(FakeBrowserDriver):
    """Counts page closes so the slot release is pinned, not assumed."""

    def __init__(self, page):
        super().__init__(page)
        self.closed_pages = 0

    async def safe_close_page(self, page):
        self.closed_pages += 1


def _real_ready_driver(page) -> _CloseRecordingDriver:
    """The fake driver with the REAL ready-state poll restored."""

    class _RealReady(_CloseRecordingDriver):
        wait_for_ready_state = BrowserDriver.wait_for_ready_state

    return _RealReady(page)


def _real_scroll_driver(page) -> _CloseRecordingDriver:
    """The fake driver with the REAL lazy-load scroll restored."""

    class _RealScroll(_CloseRecordingDriver):
        lazy_load_scroll = BrowserDriver.lazy_load_scroll

    driver = _RealScroll(page)
    driver._lazy_scroll_steps = 2
    driver._lazy_scroll_delay_seconds = 0.0
    driver._sleep = lambda _seconds: None
    return driver


def _real_flatten_driver(page) -> _CloseRecordingDriver:
    """The fake driver with the REAL shadow-DOM flatten restored."""

    class _RealFlatten(_CloseRecordingDriver):
        async def flatten_shadow_dom(self, page):
            await driver_pages.flatten_shadow_dom(page)

    return _RealFlatten(page)


def _real_consent_driver(page) -> _CloseRecordingDriver:
    """The fake driver with the REAL consent clicker restored."""

    class _RealConsent(_CloseRecordingDriver):
        async def auto_accept_cookie_consent(self, page, enabled):
            return await driver_pages.auto_accept_cookie_consent(page, enabled, self._sleep)

    driver = _RealConsent(page)
    driver._sleep = lambda _seconds: None
    return driver


async def _wedge_verdict(
    wedge: str, driver, *, include_html: bool = True, warmup_urls: list[str] | None = None
) -> tuple[CrawlResult, int]:
    """Render through the public door with the wedged read; hang-guarded.

    The outer wait_for exceeds the 5s evaluate ceiling, so a green render
    answers inside it and a red is a named failure, never a hang.
    ``warmup_urls`` routes the wedge through the warmup phase's bare
    wait_for_ready, the one readyState path no navigation budget wraps."""
    page = _WedgedReadPage(wedge)
    driver.page = page
    # extract=None restores the renderer's REAL extract path (the builder
    # otherwise stubs it), so the content wedge actually fires.
    overrides: dict[str, object] = {
        "driver": driver,
        "pin": stub_pinned_request(),
        **({"extract": None} if wedge == "content" else {}),
    }
    if warmup_urls is not None:
        overrides["warmup_urls"] = warmup_urls
    renderer = build_pinned_renderer(page, **overrides)
    if warmup_urls is not None:
        renderer._warmup_urls = warmup_urls
    options = RenderOptions(
        timeout_seconds=1,
        max_retries=0,
        include_html=include_html,
        include_text=False,
        browser_profile={"auto_accept_cookie_consent": True},
    )
    try:
        result = await asyncio.wait_for(
            renderer.render(canonical("http://example.com/"), options, policy=UNRESTRICTED_HOST_POLICY),
            timeout=_HANG_GUARD_SECONDS,
        )
    except TimeoutError:
        pytest.fail(
            f"the render outlived the {_HANG_GUARD_SECONDS}s hang guard: the {wedge} read is unbounded"
        )
    return result, driver.closed_pages


async def test_title_evaluate_cannot_wedge_the_render():
    """Round 1's pin, restated on the round-2 wedge page: the terminal
    metadata title evaluate (include_html off so no earlier read fires)."""
    result, closed = await _wedge_verdict(
        "document.title", _CloseRecordingDriver(_WedgedReadPage("document.title")), include_html=False
    )
    assert result.classification == "navigation_timeout", result.classification
    assert closed == 1


async def test_bare_content_read_cannot_wedge_the_render():
    """extract_html's page.content() carries the ceiling and its timeout
    PROPAGATES (the classifier answers navigation_timeout) -- never eaten
    into a silent html=None continue."""
    result, closed = await _wedge_verdict("content", _CloseRecordingDriver(_WedgedReadPage("content")))
    assert result.classification == "navigation_timeout", result.classification
    assert result.html is None
    assert closed == 1, "the wedged read must not strand the pooled page"


async def test_shadow_flatten_evaluate_cannot_wedge_the_render():
    """The shadow-DOM flatten -- the most expensive evaluate in the tree,
    previously bare inside suppress(Exception) -- is bounded: a wedged
    flatten degrades (suppressed) instead of parking the pooled page."""
    result, closed = await _wedge_verdict("shadowRoot", _real_flatten_driver(_WedgedReadPage("shadowRoot")))
    assert result.classification == "rendered", result.classification
    assert closed == 1


async def test_ready_state_poll_cannot_wedge_the_render():
    """The driver's readyState poll evaluate is bounded per poll; the warmup
    path's bare wait_for_ready is the unbounded route (no navigation budget
    wraps it), so the wedge rides a configured warmup URL."""
    result, closed = await _wedge_verdict(
        "readyState",
        _real_ready_driver(_WedgedReadPage("readyState")),
        warmup_urls=["http://example.com/warm"],
    )
    assert result.classification == "navigation_timeout", result.classification
    assert closed == 1


async def test_lazy_scroll_probe_cannot_wedge_the_render():
    """The lazy-load scroll's scrollHeight evaluate is bounded: a wedged
    probe degrades to an unscrolled page, never a parked render."""
    result, closed = await _wedge_verdict(
        "scrollHeight", _real_scroll_driver(_WedgedReadPage("scrollHeight"))
    )
    assert result.classification == "rendered", result.classification
    assert closed == 1


async def test_consent_fallback_evaluate_cannot_wedge_the_render():
    """The consent clicker's fallback JS scan is bounded: a wedged scan
    degrades to consent-not-clicked, never a parked render."""
    result, closed = await _wedge_verdict(
        "querySelectorAll", _real_consent_driver(_WedgedReadPage("querySelectorAll"))
    )
    assert result.classification == "rendered", result.classification
    assert closed == 1
