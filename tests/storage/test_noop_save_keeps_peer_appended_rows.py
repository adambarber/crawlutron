"""No-op save under a peer's concurrent append keeps the peer's rows.

B appends a row to A's shared-file job; A's unchanged save deletes nothing
and B's row survives. A genuine head divergence still resyncs to A's content.
"""

from __future__ import annotations

import pytest

from tests._policy_factories import job_store as store_on
from tests._storage_helpers import _make_result, _seed_history


def _results_of(store, job_id: str) -> list[dict[str, object]]:
    """The persisted results for a job, through the store's read door."""
    loaded = store.load(job_id)
    assert loaded is not None
    return loaded.results


def _captured_deletes(store, job) -> int:
    """Run one save while capturing its statements; return the DELETE count."""
    captured: list[str] = []
    store._conn.set_trace_callback(captured.append)
    try:
        store.save(job)
    finally:
        store._conn.set_trace_callback(None)
    return sum(1 for sql in captured if sql.lstrip().upper().startswith("DELETE"))


@pytest.mark.parametrize(
    ("existing", "peer_url", "expected_total"),
    [
        (20, "https://example.test/h20", 21),
        (1, "https://example.test/h1", 2),
        (0, "https://example.test/h0", 1),
    ],
)
def test_noop_save_keeps_peer_appended_row(tmp_path, existing, peer_url, expected_total):
    """Boundary matrix: empty / one / many history with a peer's appended row."""
    db = tmp_path / "jobs.db"
    store_a, store_b = store_on(db), store_on(db)

    job, expected = _seed_history(store_a, existing)

    peer = store_b.load(job.job_id)
    assert peer is not None
    peer.results.append(_make_result(peer_url))
    store_b.save(peer)

    assert _captured_deletes(store_a, job) == 0

    healed = _results_of(store_a, job.job_id)
    assert len(healed) == expected_total
    assert healed[:existing] == expected
    assert healed[existing] == _make_result(peer_url)


def test_head_divergence_still_resyncs(tmp_path):
    """Boundary: a peer's rewritten row still triggers a resync; A's content wins."""
    db = tmp_path / "jobs.db"
    store_a, store_b = store_on(db), store_on(db)

    job, expected = _seed_history(store_a, 20)

    # Peer rewrites row 5 out of band.
    peer = store_b.load(job.job_id)
    assert peer is not None
    peer.results[5] = {"url": "https://example.test/h5", "status": "rewritten-by-peer"}
    store_b.save(peer)

    assert _captured_deletes(store_a, job) == 1, "head divergence must still resync"

    healed = _results_of(store_a, job.job_id)
    assert len(healed) == 20
    assert healed == expected, "A's content wins the head-divergence resync"
