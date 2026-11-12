"""Shutdown must end extraction threads, not leave work behind (#521)."""

import asyncio
import threading
import time

import bs4
import pytest

from crawlutron.extraction import content as ext_content
from crawlutron.extraction import documents as docex
from crawlutron.extraction.content import Selectors, extract_selected
from tests._extraction_helpers import settings_with

pytestmark = pytest.mark.usefixtures("four_worker_selector_pool")

_HTML = "<html><body><p class='x'>hi</p></body></html>"


def _spawn(count, flavour, settings):
    """Caller threads driving extract_selected, so each lands a pool future."""
    return [
        threading.Thread(
            target=extract_selected,
            args=(_HTML, "http://example.com/"),
            kwargs={"selectors": Selectors(css=".x"), "settings": settings},
            name=f"{flavour}-{i}",
            daemon=True,
        )
        for i in range(count)
    ]


def _await_all(futs, seconds):
    """Poll cancellations without concurrent.futures.wait, which keeps
    waiting on CANCELLED-but-unnotified futures for the whole timeout."""
    deadline = time.monotonic() + seconds
    while not all(f.cancelled() for f in futs) and time.monotonic() < deadline:
        time.sleep(0.01)
    return all(f.cancelled() for f in futs)


def _run_shutdown_when_held(shutdown_done):
    """Shutdown in a side thread; the caller decides when to unblock workers."""
    threading.Thread(
        target=lambda: (
            asyncio.run(ext_content.shutdown_doc_extraction_executor()),
            shutdown_done.set(),
        ),
        name="shutdown",
        daemon=True,
    ).start()


def test_shutdown_refuses_queued_work_instead_of_orphaning_it(monkeypatch):
    """Queued-but-unstarted futures must be refused by shutdown, not completed
    later as orphans that keep worker threads alive past the teardown.
    """
    executed: list[int] = []
    lock = threading.Lock()
    release = threading.Event()
    saturated = threading.Barrier(5)  # 4 parked workers + this thread

    def parked_select(self, selector):
        saturated.wait(timeout=5)
        release.wait()

    def counted_select(self, selector):
        with lock:
            executed.append(1)
        return []

    monkeypatch.setattr(bs4.BeautifulSoup, "select", parked_select)
    settings = settings_with(extraction_selector_timeout=30.0)
    threads = _spawn(4, "parked", settings)  # saturate the 4 workers
    for t in threads:
        t.start()
    try:
        saturated.wait(timeout=5)
        assert not saturated.broken, "workers never saturated the pool"

        def queued_work():
            with lock:
                executed.append(1)
            return []

        executor = docex.extraction_executor()
        queued_futs = [executor.submit(queued_work) for _ in range(4)]
        shutdown_done = threading.Event()
        _run_shutdown_when_held(shutdown_done)
        refused_now = _await_all(queued_futs, 5) and not shutdown_done.is_set()
        if not refused_now:
            release.set()  # orphan watchdog: never leave the fixture parked on a bug
            shutdown_done.wait(timeout=10)
            for t in threads:
                t.join(timeout=5)
        assert refused_now, (
            "shutdown did not cancel queued-but-unstarted futures while running "
            "workers were still held; queued work was allowed to survive shutdown"
        )

        release.set()  # unpark the held workers only after refusal is proven
        assert shutdown_done.wait(timeout=10), "shutdown never finished after unparking"
        for t in threads:
            t.join(timeout=5)
        assert executed == [], (
            f"{len(executed)} queued futures ran after shutdown: shutdown must "
            "refuse queued work, not orphan it into later thread activity"
        )
    finally:
        # A failure above must still unpark the workers: the pool owns non-daemon
        # threads whose teardown shutdown(wait=True) parks forever otherwise.
        release.set()
