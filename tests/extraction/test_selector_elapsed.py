"""A timed-out running selector must return the sentinel after ~1x, not ~2x, the deadline."""

import threading
import time

import pytest

from crawlutron.extraction import documents as docex
from crawlutron.extraction.content import ExtractedContent, Selectors, extract_selected
from tests._extraction_helpers import settings_with
from tests._policy_factories import canonical

pytestmark = pytest.mark.usefixtures("four_worker_selector_pool")


@pytest.mark.real_sleeps
@pytest.mark.wall_assertion
def test_timed_out_selector_returns_within_one_deadline(monkeypatch):
    """A timed-out selector must return after one deadline, not two."""
    entered = threading.Event()
    release = threading.Event()

    def parked_find_all(root, chain):
        entered.set()
        release.wait(timeout=60)
        return []

    monkeypatch.setattr(docex, "find_all", parked_find_all)
    html = "<html><body><p class='x'>hi</p></body></html>"
    timeout = 0.5
    settings = settings_with(extraction_selector_timeout=timeout)

    begin = time.monotonic()
    try:
        result = extract_selected(
            html, canonical("http://example.com/"), selectors=Selectors(css=".x"), settings=settings
        )
    finally:
        release.set()
    elapsed = time.monotonic() - begin

    assert entered.is_set(), "the selector never started within the deadline: test raced the worker"
    assert result == ExtractedContent(None, None)
    assert elapsed < 1.5 * timeout, (
        f"a {timeout:.2f}s selector deadline blocked the caller for {elapsed:.2f}s: "
        "the defect waits a second full deadline on the still-running future"
    )
