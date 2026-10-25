"""Concurrent writers to the metrics store must not lose updates (#524).

The local RMW needs one lock; the series check-then-insert must not cross the cap.
"""

from __future__ import annotations

import sys
import threading

from app.runtime.metrics import MetricsStore
from app.storage.coordination_counters import CoordinationCounters

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

    threads = [threading.Thread(target=run) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sys.setswitchinterval(5e-3)
    assert not failures, failures


def test_concurrent_local_increments_are_never_lost():
    """One thousand increments from ten threads must read one thousand."""
    store = MetricsStore()

    _hammer(store, store.record_throttle)

    total = store.snapshot().resource_throttle_batches_total
    assert total == THREADS * INCREMENT_EACH, f"lost updates: {total} of {THREADS * INCREMENT_EACH}"


def test_concurrent_shared_store_increments_are_never_lost(tmp_path):
    """The SQL upsert path stays exact under the same hammer."""
    store = MetricsStore(counters=CoordinationCounters(tmp_path / "shared.db"))

    _hammer(store, store.record_throttle)

    total = store.snapshot().resource_throttle_batches_total
    assert total == THREADS * INCREMENT_EACH


def test_concurrent_series_registration_never_crosses_the_cap(tmp_path):
    """Callers registering past the cap while others race must fold, not overflow."""
    counters = CoordinationCounters(tmp_path / "series.db")
    store = MetricsStore(counters=counters)
    for existing in range(255):
        store.observe_crawl(caller_id=f"seed-{existing}")

    start = threading.Barrier(THREADS)
    arrivals: list[str] = []

    def register_new() -> None:
        start.wait()
        store.observe_crawl(caller_id=f"new-{threading.get_ident()}")

    threads = [threading.Thread(target=register_new) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    arrivals.extend(store.snapshot().caller_counts)
    # 256 distinct caller series + one reserved overflow series -- the same
    # bound the sequential contract already holds (test_metrics_gating A8).
    assert len(arrivals) <= 257, f"cap crossed: {len(arrivals)} distinct caller series"
    # The racy failure was N new callers minting N series past the fold: with
    # the lock the post-cap arrivals all land in the one overflow series.
    assert "__overflow__" in arrivals
