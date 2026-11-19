"""Concurrent writers to the metrics store must not lose updates (#524).

The local RMW needs one lock; the series check-then-insert must not cross the cap.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Generator, Mapping
from pathlib import Path

import pytest

from crawlutron.runtime.metrics import MetricsStore
from crawlutron.storage.coordination_counters import CoordinationCounters
from crawlutron.storage.migrations import migrate_store
from crawlutron.storage.migrations.manifest import StoreSchema
from tests._thread_fanout import run_thread_fanout, run_threads

THREADS = 10
INCREMENT_EACH = 100
_SWITCH_INTERVAL = 1e-6  # force frequent GIL yields so the dict-iteration race lands


@pytest.fixture(scope="module")
def shared_db(tmp_path_factory) -> Path:
    """One database migrated once for the whole module; tests scrub rows between themselves."""
    db_path = tmp_path_factory.mktemp("metrics_concurrency") / "shared.db"
    migrate_store(db_path, StoreSchema.COORDINATION_COUNTERS)
    return db_path


@pytest.fixture(autouse=True)
def _scrub_rows(shared_db: Path) -> Generator[None, None, None]:
    """Drop every row the test wrote so the next test starts from a clean row set."""
    yield
    store = CoordinationCounters(shared_db)
    try:
        store.reset()
    finally:
        store.close()


def _hammer(store, work) -> None:
    """Run ``work`` from ten threads released together, forcing preemption."""
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
    assert not failures, failures


async def _assert_throttle_total(store) -> None:
    _hammer(store, lambda: asyncio.run(store.record_throttle()))
    total = (await store.snapshot()).resource_throttle_batches_total
    assert total == THREADS * INCREMENT_EACH


async def test_concurrent_local_increments_are_never_lost():
    """One thousand increments from ten threads must read one thousand."""
    store = MetricsStore()
    await _assert_throttle_total(store)


async def test_concurrent_shared_store_increments_are_never_lost(shared_db):
    """The SQL upsert path stays exact under the same hammer."""
    store = MetricsStore(counters=CoordinationCounters(shared_db))
    await _assert_throttle_total(store)


async def test_concurrent_series_registration_never_crosses_the_cap(shared_db):
    """Callers registering past the cap while others race must fold, not overflow."""
    counters = CoordinationCounters(shared_db)
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


def test_snapshot_never_observes_the_flush_neither_state(shared_db):
    """Kills the two-step flush mutant: swap, release ``_lock``, then commit.
    While a batch is in flight a snapshot sees it exactly once -- pending
    before the handoff, rows after it -- never in neither place.
    """
    gate = threading.Event()
    counters = _ParkedFlush(shared_db, gate=gate)
    counters.add_pending("caller:bee", 1.0)
    assert counters.snapshot() == {"caller:bee": 1.0}

    seen: dict[str, float] = {}
    snapshot_taken = threading.Event()

    def read() -> None:
        seen.update(counters.snapshot())
        snapshot_taken.set()

    flusher = _start_parked_flush(counters)
    reader = threading.Thread(target=read)
    reader.start()
    # Mid-handoff probe: with the handoff parked, the fixed code holds the
    # write lock, so a snapshot here is the neither-state gap the mutant exposes.
    assert not snapshot_taken.wait(timeout=0.05), (
        "snapshot observed the batch in neither pending nor the rows"
    )
    gate.set()
    assert snapshot_taken.wait(timeout=5), "reader never snapshotted the post-handoff state"
    assert seen == {"caller:bee": 1.0}, "snapshot observed the batch in neither pending nor the rows"
    flusher.join(timeout=5)
    reader.join(timeout=5)
    assert not flusher.is_alive() and not reader.is_alive()


def test_reset_during_flush_ends_in_one_clean_state(shared_db):
    """reset() racing a flush must end in one clean state, never a resurrected
    batch: the handoff holds the write lock, so reset blocks until the commit.
    A reset that skips the lock finishes inside the window and resurrects it.
    """
    gate = threading.Event()
    counters = _ParkedFlush(shared_db, gate=gate)
    counters.add_pending("caller:bee", 1.0)
    reset_finished = threading.Event()

    def reset() -> None:
        counters.reset()
        reset_finished.set()

    flusher = _start_parked_flush(counters)
    resetter = threading.Thread(target=reset)
    resetter.start()
    assert not reset_finished.wait(timeout=0.05), "reset returned while the flush held the handoff"
    gate.set()
    assert reset_finished.wait(timeout=5), "reset never finished after the handoff released"
    flusher.join(timeout=5)
    resetter.join(timeout=5)
    assert not flusher.is_alive() and not resetter.is_alive()
    assert counters.snapshot() == {}, "reset during a flush left a trace of the batch"


def test_second_flush_of_an_empty_batch_is_a_no_op(shared_db):
    """A second flush after the first commits nothing; pending is drained once."""
    counters = _ParkedFlush(shared_db, gate=None)
    counters.add_pending("bee", 2.0)
    counters.flush()
    counters.flush()
    assert counters.snapshot() == {"bee": 2.0}


def test_series_cap_holds_under_forced_flush_window(shared_db):
    """Ten registrations race while every flush is parked mid-handoff: a
    snapshot that sees a batch in neither pending nor the rows lets each
    registration mint its own series past 257; the atomic handoff keeps the cap."""
    gate = threading.Event()
    gate.set()  # open while seeding: flushes commit normally
    counters = _ParkedFlush(shared_db, gate=gate)
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


async def test_caller_series_source_read_runs_off_the_loop(shared_db):
    """The caller-series source read runs off the event loop.

    Red before step 2: snapshot() ran on the loop thread; green after it runs
    on a worker thread via asyncio.to_thread.
    """
    loop_ident = threading.get_ident()
    counters = _RecordingSnapshot(shared_db)
    store = MetricsStore(counters=counters)
    await store.observe_crawl(caller_id="off-loop")
    assert counters.snapshot_idents, "store.snapshot was never called"
    assert all(ident != loop_ident for ident in counters.snapshot_idents)


async def test_bind_holds_write_lock_across_carryover(shared_db) -> None:
    """An increment parked mid-RMW while bind carries over ``_local`` is
    flushed with the carryover: bind holds the write lock, so nothing is
    stranded; unfixed, bind clears ``_local`` mid-RMW and reads 1.0 instead of 2.0."""
    KEY = "extraction_doc_pool_unavailable_total"
    store = MetricsStore()  # no store bound yet
    store.record_doc_pool_unavailable()  # _local[KEY] = 1.0
    counters = CoordinationCounters(shared_db)

    parked = threading.Event()
    released = threading.Event()
    incrementer = threading.Thread(target=store.parked_local_increment, args=(KEY, parked, released))
    bind_worker = threading.Thread(target=lambda: asyncio.run(store.bind_counters(counters)))
    incrementer.start()
    assert parked.wait(timeout=5), "the increment never parked mid read-modify-write"
    bind_worker.start()  # fixed: blocks behind the parked increment's lock; unfixed: races and loses it
    released.set()
    incrementer.join(timeout=5)
    bind_worker.join(timeout=5)
    assert not incrementer.is_alive() and not bind_worker.is_alive()
    assert (await store.snapshot()).extraction_doc_pool_unavailable_total == 2.0, (
        "an increment landing mid-bind was stranded and lost"
    )


async def test_repeat_caller_reads_store_once(shared_db) -> None:
    """Repeat observations of one caller read the shared table once, not per
    call: the admitted-series cache answers for known callers. A new caller
    still consults the store and folds at the cap."""
    counters = _RecordingSnapshot(shared_db)
    store = MetricsStore(counters=counters)
    for _ in range(3):
        await store.observe_crawl(caller_id="repeat")
    assert len(counters.snapshot_idents) == 1, f"repeat caller re-read the store: {counters.snapshot_idents}"
    for i in range(300):
        await store.observe_crawl(caller_id=f"new-{i}")
    counts = (await store.snapshot()).caller_counts
    assert len(counts) <= 257
    assert "__overflow__" in counts
    assert len(counters.snapshot_idents) > 1  # new callers still consult the store


async def test_caller_cache_is_cleared_on_reset(shared_db) -> None:
    """The admitted-series cache is cleared on reset, so a reset caller
    re-consults the shared table."""
    counters = _RecordingSnapshot(shared_db)
    store = MetricsStore(counters=counters)
    await store.observe_crawl(caller_id="repeat")
    assert len(counters.snapshot_idents) == 1
    await store.observe_crawl(caller_id="repeat")  # cached: no further read
    assert len(counters.snapshot_idents) == 1
    store.reset()  # clears the admitted-series cache
    await store.observe_crawl(caller_id="repeat")  # new table state: re-consults
    assert len(counters.snapshot_idents) == 2


async def test_caller_cache_is_cleared_on_bind(shared_db) -> None:
    """The admitted-series cache is cleared on bind: carryover flushes into the
    rebound store and a repeat caller re-consults it."""
    store = MetricsStore()  # no store bound yet
    await store.observe_crawl(caller_id="repeat")  # _local holds caller:repeat = 1
    bound = _RecordingSnapshot(shared_db)
    await store.bind_counters(bound)  # carryover flushes _local into bound
    await store.observe_crawl(caller_id="repeat")  # cache cleared: re-consults bound
    assert len(bound.snapshot_idents) == 1  # exactly one re-consultation
    assert (await store.snapshot()).caller_counts == {"repeat": 2.0}  # carryover + new


async def test_admitted_series_count_stays_at_cap(shared_db) -> None:
    """A caller-id flood cannot grow the admitted-series dict past the cap:
    folded callers are not cached, so the cache stops at the cap while the
    overflow series keeps bare == sum(labels) exact."""
    counters = CoordinationCounters(shared_db)
    store = MetricsStore(counters=counters)
    for i in range(400):
        await store.observe_crawl(caller_id=f"flood-{i}")
    assert store.admitted_series_count() == 256
    counts = (await store.snapshot()).caller_counts
    assert len(counts) == 257
    assert counts["__overflow__"] >= 1.0
    await store.observe_crawl(caller_id="flood-7")  # repeat caller: cache hit
    assert store.admitted_series_count() == 256


async def test_concurrent_exposition_never_sees_a_changing_dict() -> None:
    """Concurrent increments racing /metrics snapshots must not raise
    RuntimeError: dictionary changed size during iteration, and every snapshot
    is one consistent point-in-time (bare total equals the sum of its labels)."""
    store = MetricsStore()
    errors: list[BaseException] = []
    barrier = threading.Barrier(12)
    previous = sys.getswitchinterval()
    sys.setswitchinterval(_SWITCH_INTERVAL)
    try:

        def worker(index: int) -> None:
            barrier.wait()
            ident = threading.get_ident()
            try:

                async def spin() -> None:
                    for step in range(60):
                        if index < 6:
                            await store.observe_crawl(caller_id=f"caller-{index}-{ident}-{step}")
                        else:
                            text = await store.as_prometheus(caller_detail=True)
                            assert "crawler_crawl_requests_total " in text

                asyncio.run(spin())
            except BaseException as exc:  # the failure IS the result
                errors.append(exc)

        run_thread_fanout(worker, count=12)
        assert not errors, errors

        record = await store.snapshot()
        assert record.crawl_requests_total == sum(record.caller_counts.values()), record
    finally:
        sys.setswitchinterval(previous)
