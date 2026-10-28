"""A timed-out selector's worker must not outlive extraction teardown (#560).

The #521 cancel leaves a *started* runaway running on the shared extraction
executor: ``extract_selected`` returns, and the thread keeps executing until
the pathological selector finishes on its own. After
``shutdown_doc_extraction_executor`` that thread is still alive -- exactly the
unbounded-lifetime violation the ticket names. The probe: park a select in the
timeout path, shut extraction down, and assert no ``extraction``-prefixed
thread survives the shutdown.
"""

import asyncio
import threading

import bs4
import pytest

from app.extraction import documents
from app.extraction.documents import ExtractedContent, Selectors, extract_selected
from tests._extraction_helpers import settings_with

pytestmark = pytest.mark.usefixtures("four_worker_selector_pool")


def _live_extraction_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("extraction")]


@pytest.mark.real_sleeps
def test_timed_out_selector_thread_does_not_outlive_teardown(monkeypatch):
    """Shutdown must await (drain) the in-flight timed-out evaluation: no
    ``extraction`` worker thread may be alive once the shutdown returns."""
    release = threading.Event()

    def parked_select(self, selector):
        release.wait(timeout=15)

    monkeypatch.setattr(bs4.BeautifulSoup, "select", parked_select)
    settings = settings_with(extraction_selector_timeout=0.05)

    result = extract_selected(
        "<html><body><p class='x'>hi</p></body></html>",
        "http://example.com/",
        selectors=Selectors(css=".x"),
        settings=settings,
    )
    assert result == ExtractedContent(None, None)
    assert _live_extraction_threads(), "no extraction worker ever ran the select"

    # The helper releases the parked select just after shutdown begins: the fixed
    # drain awaits the in-flight evaluation; the broken one returns, thread parked.
    threading.Timer(0.1, release.set).start()
    asyncio.run(documents.shutdown_doc_extraction_executor())

    assert _live_extraction_threads() == [], (
        f"{len(_live_extraction_threads())} extraction worker thread(s) alive "
        "after shutdown: the timed-out selector's future was abandoned past "
        "teardown instead of awaited"
    )
