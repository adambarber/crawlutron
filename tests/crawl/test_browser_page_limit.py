"""Throughput regression test: concurrent browser renders are capped.

Each render opens a browser tab; without a cap a burst of N requests opens N
tabs and crashes/OOMs the shared Camoufox process.
"""

import asyncio
import contextlib
from functools import partial

from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions
from tests._browser_doubles import DEFAULT_PAGE_HTML, ScriptedPage, scripted_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


async def _track_overlapping_tabs(page, *, tracker, saturated):
    """Count the tabs open at the same moment, which is the cap's whole job.

    A serializing cap reports 1, an ignored one 6; a single ``sleep(0)`` was a
    race under load, so holding the tab open until ``saturated`` is deterministic.
    """
    tracker["n"] += 1
    tracker["max"] = max(tracker["max"], tracker["n"])
    if tracker["n"] >= tracker["expected"]:
        saturated.set()
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(saturated.wait(), timeout=5)
    finally:
        tracker["n"] -= 1


def _capped_page(tracker, saturated):
    return ScriptedPage(
        url="http://example.com/",
        on_goto=partial(_track_overlapping_tabs, tracker=tracker, saturated=saturated),
        answers={"readyState": "complete", "document.title": "Fake Title"},
        html=DEFAULT_PAGE_HTML,
    )


class _FakeBrowser:
    def __init__(self, tracker, saturated):
        self._tracker = tracker
        self._saturated = saturated

    async def new_page(self, **kwargs):
        return _capped_page(self._tracker, self._saturated)

    @property
    def is_closed(self):
        return False


async def test_concurrent_page_renders_capped(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_MAX_CONCURRENT_PAGES", "2")
    saturated = asyncio.Event()
    tracker = {"n": 0, "max": 0, "expected": 2}
    renderer = scripted_renderer(_FakeBrowser(tracker, saturated))
    try:
        opts = RenderOptions(browser_profile={})
        await asyncio.gather(
            *[
                renderer.render(
                    NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY),
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
