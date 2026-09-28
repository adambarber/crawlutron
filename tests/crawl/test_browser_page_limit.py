"""Throughput regression test: concurrent browser renders are capped.

Each render opens a browser tab; without a cap a burst of N requests opens N
tabs and crashes/OOMs the shared Camoufox process.
"""

import asyncio
import contextlib
from functools import partial

from app.models import RenderOptions
from tests._browser_doubles import DEFAULT_PAGE_HTML, ScriptedPage, scripted_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


async def _track_overlapping_tabs(page, *, tracker, saturated):
    """Count the tabs open at the same moment, which is the cap's whole job.

    The cap decides how many tabs may be open *concurrently*, and
    ``tracker["max"]`` is that number. A cap that serialized renders entirely
    would report 1, and a cap that was ignored would report 6.

    Holding the tab open until ``saturated`` is set is what makes that a fact
    instead of a race. Measuring overlap at a single ``asyncio.sleep(0)`` --
    the earlier version of this helper -- looked correct and was not: when the
    event loop is loaded, the first holder is resumed and decrements before the
    second is ever scheduled inside the window, so a cap of 2 reported 1 and the
    test failed roughly one run in five without anything being wrong. A cap of
    N admits N waiters, so waiting for N of them to be inside is deterministic.

    The wait is bounded, and a timeout is swallowed rather than raised, so that
    "the cap serialized everything" arrives as the assertion below it failing
    with a readable tracker instead of a bare ``TimeoutError`` from here.
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
    monkeypatch.setenv("BROWSER_CRAWLER_MAX_CONCURRENT_PAGES", "2")
    saturated = asyncio.Event()
    tracker = {"n": 0, "max": 0, "expected": 2}
    renderer = scripted_renderer(_FakeBrowser(tracker, saturated))
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
