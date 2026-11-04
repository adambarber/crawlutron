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
