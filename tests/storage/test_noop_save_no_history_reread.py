"""A no-op batch save must not re-read the job's full history (#980).

History-independence, not a byte cap: digest work must not grow with history.
"""

from __future__ import annotations

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store_accumulators import JobAccumulators
from tests._policy_factories import canonical
from tests._policy_factories import job_store as store_on


def _job_with(store, pages: int):
    """A saved job holding ``pages`` results and visited URLs."""
    job = store.create("https://example.test/", CrawlPolicy())
    for i in range(pages):
        url = f"https://example.test/h{i}"
        job.results.append({"url": url, "status": "success", "body": "x" * 500})
        job.visited.add(canonical(url))
    store.save(job)
    return job


def _noop_cost(store, job) -> int:
    """Bytes fed to the digest by one no-op save of an unchanged job."""
    accumulator: JobAccumulators = store._accumulators
    before = accumulator.digest_work_bytes()
    store.save(job)
    return accumulator.digest_work_bytes() - before


def test_noop_save_cost_does_not_grow_with_history(tmp_path):
    """A 10x larger history must not make a no-op save 10x more expensive."""
    small_store = store_on(tmp_path / "small.db")
    large_store = store_on(tmp_path / "large.db")

    small = _noop_cost(small_store, _job_with(small_store, 100))
    large = _noop_cost(large_store, _job_with(large_store, 1000))

    # Constant work, not O(history): the two costs differ by at most one page.
    assert large <= small + 2000, (
        f"no-op save cost grew with history: {small} bytes at 100 pages, {large} bytes at 1000 pages"
    )
    small_store._conn.close()
    large_store._conn.close()


def test_noop_save_cost_is_far_below_a_full_digest(tmp_path):
    """A no-op save must not digest the history it already persisted."""
    store = store_on(tmp_path / "jobs.db")
    try:
        cost = _noop_cost(store, _job_with(store, 500))
        # One page is ~520 bytes serialized; a full-history digest is ~260 KiB.
        assert cost < 5000, f"no-op save digested the whole history ({cost} bytes)"
    finally:
        store._conn.close()


def test_three_consecutive_noops_stay_constant(tmp_path):
    """Repeated saves of an unchanged job keep the same small cost."""
    store = store_on(tmp_path / "jobs.db")
    try:
        job = _job_with(store, 500)
        costs = [_noop_cost(store, job) for _ in range(3)]
        assert max(costs) < 5000, f"no-op saves grew: {costs}"
    finally:
        store._conn.close()
