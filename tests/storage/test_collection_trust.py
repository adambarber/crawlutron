"""Collection trust: a save must trust the table, not process-local memory.

#314 F2: the C7 digest skips a save when the content matches what THIS
process last wrote; an out-of-band table change was never healed.
"""

from __future__ import annotations

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import FrontierEntry, OrderedUrlSet
from tests._policy_factories import canonical
from tests._policy_factories import job_store as store_on

# Disjoint per collection: the load's cross-list identity merge drops a
# frontier entry whose URL is visited, so collections must not share URLs.
_COLLECTION_URLS = {
    "results": ("https://example.com/x1", "https://example.com/x2"),
    "visited": ("https://example.com/x3", "https://example.com/x4"),
    "frontier": ("https://example.com/x5", "https://example.com/x6"),
}


def _job_holding(store, collection: str, urls):
    """A job whose one named collection holds exactly ``urls``."""
    job = store.create("https://example.com/", CrawlPolicy())
    if collection == "results":
        job.results = [{"url": url, "status": "success"} for url in urls]
    elif collection == "visited":
        job.visited = OrderedUrlSet([canonical(url) for url in urls])
    else:
        job.frontier = [FrontierEntry(url=canonical(url), depth=0) for url in urls]
    return job


def _collection(state, name):
    if name == "results":
        return state.results
    if name == "visited":
        return set(state.visited)
    return state.frontier


@pytest.mark.parametrize("collection", ["results", "visited", "frontier"])
def test_a_stale_process_resyncs_out_of_band_collection_changes(tmp_path, collection):
    """A's skip must not fire when the table holds different content.

    A saves [x, x]; a sibling store shrinks that collection's table to [y]; A
    re-saves its unchanged state: the table must heal back to A's content."""
    db = tmp_path / "jobs.db"
    store_a = store_on(db)
    job = _job_holding(store_a, collection, _COLLECTION_URLS[collection])
    store_a.save(job)

    store_b = store_on(db)
    sibling = store_b.load(job.job_id)
    assert sibling is not None
    y = "https://example.com/y1"
    sibling.results = [{"url": y, "status": "success"}]
    sibling.visited = OrderedUrlSet([canonical(y)])
    sibling.frontier = [FrontierEntry(url=canonical(y), depth=0)]
    store_b.save(sibling)

    store_a.save(job)  # unchanged in-memory collection, table holds [y1]

    healed = store_b.load(job.job_id)
    assert healed is not None
    assert _collection(healed, collection) == _collection(job, collection), (
        f"{collection}: the out-of-band change was never healed"
    )


def test_load_does_not_mix_row_and_collection_generations(tmp_path, monkeypatch):
    """A load returns one generation: row fields and collections as of one point.

    #314 F3: the row select released the lock before the collection reads; a save
    landing in between mixed a stale row with newer collections."""
    import threading

    from crawlutron.models.crawl_policy import CrawlPolicy

    db = tmp_path / "jobs.db"
    store = store_on(db)
    job = store.create("https://example.com/", CrawlPolicy())
    job.results = [{"url": "https://example.com/p0", "status": "success"}]
    store.save(job)

    real_row_to_job = store._row_to_job
    barrier = threading.Event()
    resumed = threading.Event()

    def interleaving_row_to_job(row, **kwargs):
        """Between the row select and the collection reads, save from another thread."""
        if kwargs.get("with_accumulators") and not barrier.is_set():
            barrier.set()
            resumed.wait(5)
        return real_row_to_job(row, **kwargs)

    monkeypatch.setattr(store, "_row_to_job", interleaving_row_to_job)

    def sibling_save():
        if not barrier.wait(5):
            return
        sibling_store = store_on(db)
        sibling = sibling_store.load(job.job_id)
        assert sibling is not None
        sibling.stats = {"total_crawled": 99}
        sibling.results = sibling.results + [{"url": "https://example.com/p1", "status": "success"}]
        sibling_store.save(sibling)
        resumed.set()

    writer = threading.Thread(target=sibling_save)
    writer.start()
    loaded = store.load(job.job_id)
    writer.join()

    # One generation: the loaded stats row and results collection agree. A save
    # between row-select and collection-reads must not mix the two generations.
    assert loaded is not None
    mixed = loaded.stats.get("total_crawled") == 0 and len(loaded.results) == 2
    assert not mixed, "load mixed a stale row with newer collections"
