"""A timed-out selector must free its worker, not pin the pool (#521)."""

import concurrent.futures
import threading
import time

import bs4
import pytest

from app.extraction import documents as docex
from app.extraction.content import (
    ExtractedContent,
    Selectors,
    extract_selected,
)
from tests._extraction_helpers import settings_with

pytestmark = pytest.mark.usefixtures("four_worker_selector_pool")


def test_timed_out_selectors_are_cancelled_not_orphaned(monkeypatch):
    """Queued selectors behind a saturated pool must be cancelled on timeout,
    not started later as orphans that hog workers. Eight pages, four workers,
    a parked select, a 0.05s deadline: the last four never run unless orphaned.
    """
    calls: list[int] = []
    lock = threading.Lock()
    all_running = threading.Event()
    saturated = threading.Barrier(4, action=all_running.set)  # the 4 workers only
    release = threading.Event()

    def parked_select(self, selector):
        with lock:
            calls.append(1)
        saturated.wait(timeout=5)
        release.wait()

    monkeypatch.setattr(bs4.BeautifulSoup, "select", parked_select)
    html = "<html><body><p class='x'>hi</p></body></html>"
    settings = settings_with(extraction_selector_timeout=0.05)
    executor = docex.extraction_executor()
    real_submit = executor.submit
    captured: list[concurrent.futures.Future] = []

    def capturing_submit(fn, /, *args, **kwargs):
        fut = real_submit(fn, *args, **kwargs)
        captured.append(fut)
        return fut

    monkeypatch.setattr(executor, "submit", capturing_submit)
    for _ in range(8):
        result = extract_selected(
            html, "http://example.com/", selectors=Selectors(css=".x"), settings=settings
        )
        assert result == ExtractedContent(None, None)
    assert len(calls) == 4, "the saturated pool did not hold exactly the first four"
    release.set()  # orphans would now start; the census below is deterministic
    deadline = time.monotonic() + 5
    while not all(fut.done() for fut in captured) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(fut.done() for fut in captured), "queued selectors neither finished nor cancelled"
    assert len(calls) == 4, (
        f"{len(calls)} selector evaluations ran: anything past four means the "
        "submissions queued behind the saturated pool started later as orphans"
    )
