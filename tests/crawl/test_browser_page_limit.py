"""Throughput regression test: concurrent browser renders are capped.

Each render opens a browser tab; without a cap a burst of N requests opens N
tabs and crashes/OOMs the shared Camoufox process.
"""

import asyncio
from functools import partial

from _browser_doubles import DEFAULT_PAGE_HTML, ScriptedPage, scripted_renderer
from _policy_factories import UNRESTRICTED_HOST_POLICY

from app.models import RenderOptions


async def _track_overlapping_tabs(page, *, tracker):
    """Count the tabs open at the same moment, which is the cap's whole job.

    A scheduling point, not a duration: the cap decides how many tabs may be
    open *concurrently*, and ``tracker["max"]`` is that number. A cap that
    serialized renders entirely would report 1, and a cap that ignored the cap
    would report 6, with no stopwatch involved.
    """
    tracker["n"] += 1
    tracker["max"] = max(tracker["max"], tracker["n"])
    await asyncio.sleep(0)
    tracker["n"] -= 1


def _capped_page(tracker):
    return ScriptedPage(
        url="http://example.com/",
        on_goto=partial(_track_overlapping_tabs, tracker=tracker),
        answers={"readyState": "complete", "document.title": "Fake Title"},
        html=DEFAULT_PAGE_HTML,
    )


class _FakeBrowser:
    def __init__(self, tracker):
        self._tracker = tracker

    async def new_page(self, **kwargs):
        return _capped_page(self._tracker)

    @property
    def is_closed(self):
        return False


async def test_concurrent_page_renders_capped(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_MAX_CONCURRENT_PAGES", "2")
    tracker = {"n": 0, "max": 0}
    renderer = scripted_renderer(_FakeBrowser(tracker))
    try:
        opts = RenderOptions(browser_profile={})
        await asyncio.gather(
            *[
                renderer.render(
                    "http://example.com/",
                    opts,
                    policy=UNRESTRICTED_HOST_POLICY,
                )
                for _ in range(6)
            ]
        )
    finally:
        await renderer.close()
    assert tracker["max"] <= 2, f"page cap ignored: {tracker}"
    assert tracker["max"] >= 2, f"cap serialized renders entirely: {tracker}"
