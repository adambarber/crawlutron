"""Concurrent writers to the metrics store must not lose updates (#524).

The local RMW needs one lock; the series check-then-insert must not cross the cap.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Mapping

from crawlutron.runtime.metrics import MetricsStore
from crawlutron.storage.coordination_counters import CoordinationCounters
from tests._thread_fanout import run_threads

THREADS = 10
INCREMENT_EACH = 100


def _hammer(store, work) -> None:
    """Run ``work`` from ten threads released together, forcing preemption."""
    sys.setswitchinterval(1e-6)
    start = threading.Barrier(THREADS)
    failures: list[Exception] = []

    def run() -> None:
        try:
            start.wait()
            for _ in range(INCREMENT_EACH):
                work()
        except Exception as exc:  # the failure IS the result
            failures.append(exc)

    run_threads(run, count=THREADS)
    sys.setswitchinterval(5e-3)
    assert not failures, failures


async def _assert_throttle_total(store) -> None:
    _hammer(store, lambda: asyncio.run(store.record_throttle()))
    total = (await store.snapshot()).resource_throttle_batches_total
    assert total == THREADS * INCREMENT_EACH


async def test_concurrent_local_increments_are_never_lost():
    """One thousand increments from ten threads must read one thousand."""
    store = MetricsStore()
    await _assert_throttle_total(store)


async def test_concurrent_shared_store_increments_are_never_lost(tmp_path):
    """The SQL upsert path stays exact under the same hammer."""
    store = MetricsStore(counters=CoordinationCounters(tmp_path / "shared.db"))
    await _assert_throttle_total(store)


async def test_concurrent_series_registration_never_crosses_the_cap(tmp_path):
    """Callers registering past the cap while others race must fold, not overflow."""
    counters = CoordinationCounters(tmp_path / "series.db")
    store = MetricsStore(counters=counters)
    for existing in range(255):
        await store.observe_crawl(caller_id=f"seed-{existing}")

    start = threading.Barrier(THREADS)
    arrivals: list[str] = []

    def register_new() -> None:
        start.wait()
        asyncio.run(store.observe_crawl(caller_id=f"new-{threading.get_ident()}"))

    run_threads(register_new, count=THREADS)

    arrivals.extend((await store.snapshot()).caller_counts)
    # 256 distinct caller series + one reserved overflow series -- the same
    # bound the sequential contract already holds (test_metrics_gating A8).
    assert len(arrivals) <= 257, f"cap crossed: {len(arrivals)} distinct caller series"
    # The racy failure was N new callers minting N series past the fold: with
    # the lock the post-cap arrivals all land in the one overflow series.
    assert "__overflow__" in arrivals


class _ParkedFlush(CoordinationCounters):
    """A counters store whose add_many pauses between the swap and the commit,
    forcing the flush handoff window wide open; every wait is bounded.
    """

    def __init__(self, *args, gate=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate
        self.parked = threading.Event()

    def add_many(self, deltas: Mapping[str, float]) -> None:
        if self._gate is not None:
            self.parked.set()
            self._gate.wait(timeout=5)
            self.parked.clear()
        return super().add_many(deltas)


def _start_parked_flush(counters: _ParkedFlush) -> threading.Thread:
    """Start a flush on a worker thread and wait until it is parked mid-handoff."""
    flusher = threading.Thread(target=counters.flush)
    flusher.start()
    assert counters.parked.wait(timeout=5), "flush did not enter the handoff window"
    return flusher


def test_snapshot_never_observes_the_flush_neither_state(tmp_path):
    """Kills the two-step flush mutant: swap, release ``_lock``, then commit.
    While a batch is in flight a snapshot sees it exactly once -- pending
    before the handoff, rows after it -- never in neither place.
    """
    gate = threading.Event()
    counters = _ParkedFlush(tmp_path / "handoff.db", gate=gate)
    counters.add_pending("caller:bee", 1.0)
    assert counters.snapshot() == {"caller:bee": 1.0}

    seen: dict[str, float] = {}

    def read() -> None:
        seen.update(counters.snapshot())

    flusher = _start_parked_flush(counters)
    reader = threading.Thread(target=read)
    reader.start()
    reader.join(timeout=2)
    assert not reader.is_alive() or "caller:bee" not in seen, (
        "snapshot observed the batch in neither pending nor the rows"
    )
    gate.set()
    flusher.join(timeout=5)
    reader.join(timeout=5)
    assert not flusher.is_alive() and not reader.is_alive()
    assert seen == {"caller:bee": 1.0}, f"batch seen {seen!r}, expected exactly once"


def test_reset_during_flush_ends_in_one_clean_state(tmp_path):
    """reset() racing a flush must end in one clean state: the commit must not
    resurrect data reset dropped, and reset must not drop a committed batch.
    """
    gate = threading.Event()
    counters = _ParkedFlush(tmp_path / "reset.db", gate=gate)
    counters.add_pending("caller:bee", 1.0)
    flusher = _start_parked_flush(counters)
    counters.reset()
    gate.set()
    flusher.join(timeout=5)
    assert not flusher.is_alive()
    assert counters.snapshot() == {}, "reset during a flush left a trace of the batch"


def test_second_flush_of_an_empty_batch_is_a_no_op(tmp_path):
    """A second flush after the first commits nothing; pending is drained once."""
    counters = _ParkedFlush(tmp_path / "double.db", gate=None)
    counters.add_pending("bee", 2.0)
    counters.flush()
    counters.flush()
    assert counters.snapshot() == {"bee": 2.0}


def test_series_cap_holds_under_forced_flush_window(tmp_path):
    """Ten registrations race while every flush is parked mid-handoff: a
    snapshot that sees a batch in neither pending nor the rows lets each
    registration mint its own series past 257; the atomic handoff keeps the cap."""
    gate = threading.Event()
    gate.set()  # open while seeding: flushes commit normally
    counters = _ParkedFlush(tmp_path / "series.db", gate=gate)
    store = MetricsStore(counters=counters)
    for existing in range(255):
        asyncio.run(store.observe_crawl(caller_id=f"seed-{existing}"))
    gate.clear()  # close: racing flushes park mid-handoff

    def register_new(index: int) -> None:
        asyncio.run(store.observe_crawl(caller_id=f"new-{index}"))

    threads = [threading.Thread(target=register_new, args=(index,)) for index in range(THREADS)]
    for thread in threads:
        thread.start()
    # Hold the window: every racing snapshot lands while batches are in
    # flight; then release -- every wait is bounded so this cannot hang.
    assert counters.parked.wait(timeout=5), "no registration reached the handoff window"
    time.sleep(0.25)
    gate.set()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a registration did not return"

    arrivals = list(asyncio.run(store.snapshot()).caller_counts)
    assert len(arrivals) <= 257, f"cap crossed: {len(arrivals)} distinct caller series"
    assert "__overflow__" in arrivals, "post-cap arrivals did not fold into the overflow series"


class _RecordingSnapshot(CoordinationCounters):
    """A counters double that records the thread ident of every snapshot() call."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.snapshot_idents: list[int] = []

    def snapshot(self) -> dict[str, float]:
        self.snapshot_idents.append(threading.get_ident())
        return super().snapshot()


async def test_caller_series_source_read_runs_off_the_loop(tmp_path):
    """The caller-series source read runs off the event loop.

    Red before step 2: snapshot() ran on the loop thread; green after it runs
    on a worker thread via asyncio.to_thread.
    """
    loop_ident = threading.get_ident()
    counters = _RecordingSnapshot(tmp_path / "offloop.db")
    store = MetricsStore(counters=counters)
    await store.observe_crawl(caller_id="off-loop")
    assert counters.snapshot_idents, "store.snapshot was never called"
    assert all(ident != loop_ident for ident in counters.snapshot_idents)
