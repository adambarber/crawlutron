"""Shutdown must end extraction threads, not leave work behind (#521)."""

import asyncio
import threading
import time

import bs4
import pytest

from app.extraction import content as ext_content
from app.extraction.content import Selectors, extract_selected
from tests._extraction_helpers import settings_with


@pytest.mark.real_sleeps
def test_shutdown_refuses_queued_work_instead_of_orphaning_it(monkeypatch):
    """Queued-but-unstarted futures must be refused by shutdown, not completed
    later as orphans that keep worker threads alive past the teardown.
    """
    executed: list[int] = []
    lock = threading.Lock()
    release = threading.Event()

    def parked_select(self, selector):
        release.wait(timeout=15)

    def counted_select(self, selector):
        with lock:
            executed.append(1)
        return []

    monkeypatch.setattr(bs4.BeautifulSoup, "select", parked_select)
    html = "<html><body><p class='x'>hi</p></body></html>"
    settings = settings_with(extraction_selector_timeout=30.0)

    def spawn(count, flavour):
        return [
            threading.Thread(
                target=extract_selected,
                args=(html, "http://example.com/"),
                kwargs={"selectors": Selectors(css=".x"), "settings": settings},
                name=f"{flavour}-{i}",
            )
            for i in range(count)
        ]

    threads = spawn(4, "parked")  # saturate the 4 workers
    for t in threads:
        t.start()
    time.sleep(0.3)  # let the parked selects take all four workers

    monkeypatch.setattr(bs4.BeautifulSoup, "select", counted_select)
    queued = spawn(4, "queued")  # these land queued behind the parked ones
    for t in queued:
        t.start()
    time.sleep(0.3)  # let the submissions queue

    asyncio.run(ext_content.shutdown_doc_extraction_executor())
    monkeypatch.setattr(bs4.BeautifulSoup, "select", counted_select)
    release.set()  # unpark; orphaned queued work would now run
    time.sleep(1.0)
    for t in threads + queued:
        t.join(timeout=5)
    assert executed == [], (
        f"{len(executed)} queued futures ran after shutdown: shutdown must "
        "refuse queued work, not orphan it into later thread activity"
    )
