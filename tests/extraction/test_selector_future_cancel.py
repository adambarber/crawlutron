"""A timed-out selector must free its worker, not pin the pool (#521)."""

import threading
import time

import bs4
import pytest

from app.extraction.content import ExtractedContent, Selectors, extract_selected
from tests._extraction_helpers import settings_with


@pytest.mark.real_sleeps
def test_timed_out_selectors_are_cancelled_not_orphaned(monkeypatch):
    """Queued selectors behind a saturated pool must be cancelled on timeout,
    not started later as orphans that hog workers. Eight pages, four workers,
    a 2s select, a 0.05s deadline: the last four never run unless orphaned.
    """
    calls: list[int] = []
    lock = threading.Lock()

    def counting_select(self, selector):
        with lock:
            calls.append(1)
        time.sleep(2.0)
        return []

    monkeypatch.setattr(bs4.BeautifulSoup, "select", counting_select)
    html = "<html><body><p class='x'>hi</p></body></html>"
    settings = settings_with(extraction_selector_timeout=0.05)
    for _ in range(8):
        result = extract_selected(
            html, "http://example.com/", selectors=Selectors(css=".x"), settings=settings
        )
        assert result == ExtractedContent(None, None)
    time.sleep(3.5)  # orphans would all have started and finished by now
    assert len(calls) <= 4, (
        f"{len(calls)} selector evaluations ran (some workers may still be drained "
        "by earlier tests); anything past four means the submissions queued behind "
        "the saturated pool started later as orphans"
    )
