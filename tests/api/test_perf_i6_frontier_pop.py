"""Perf item 6 (RED): frontier popping must be O(1) without a schema change.

``_pop_frontier`` slices + ``del``s the persisted list per batch (O(n)
memmove each time -> quadratic over a wide crawl). Popping must advance a
transient head offset instead, compacting the consumed prefix only
periodically, while the persisted JSON row stays a plain URL list.
"""

from app.api.job_runner import BATCH_SIZE, _frontier_add, _frontier_set, _pop_frontier
from app.crawl.job_store import CrawlJobState


def _job(urls: list[str]) -> CrawlJobState:
    return CrawlJobState(job_id="j", frontier=list(urls), visited=set())


def _urls(n: int) -> list[str]:
    return [f"http://example.com/page{i}" for i in range(n)]


def test_pop_does_not_memmove_the_list():
    """Popping leaves the persisted list object intact (no slice+del)."""
    job = _job(_urls(50))
    before = job.frontier
    batch = _pop_frontier(job)
    assert batch == _urls(50)[:BATCH_SIZE]
    assert job.frontier is before
    assert len(job.frontier) == 50


def test_pop_advances_fifo_across_calls():
    job = _job(_urls(3 * BATCH_SIZE))
    first = _pop_frontier(job)
    second = _pop_frontier(job)
    assert first == _urls(3 * BATCH_SIZE)[:BATCH_SIZE]
    assert second == _urls(3 * BATCH_SIZE)[BATCH_SIZE : 2 * BATCH_SIZE]
    assert first != second


def test_pop_skips_visited_without_refetch():
    """Already-visited slots (stale prefix after a reload) are skipped, not returned."""
    job = _job(_urls(2 * BATCH_SIZE))
    job.visited.update(_urls(2 * BATCH_SIZE)[:BATCH_SIZE])
    batch = _pop_frontier(job)
    assert batch == _urls(2 * BATCH_SIZE)[BATCH_SIZE : 2 * BATCH_SIZE]


def test_pop_returns_empty_on_empty_frontier():
    job = _job([])
    assert _pop_frontier(job) == []


def test_pop_compacts_consumed_prefix_periodically():
    """The consumed prefix is reclaimed (bounded growth) without changing shape."""
    from app.api import job_runner as runner

    threshold = runner._FRONTIER_COMPACT_THRESHOLD
    job = _job(_urls(threshold + BATCH_SIZE))
    pops = 0
    while len(job.frontier) == threshold + BATCH_SIZE and pops < threshold:
        _pop_frontier(job)
        pops += 1
    assert len(job.frontier) < threshold + BATCH_SIZE
    assert isinstance(job.frontier, list)
    assert all(isinstance(u, str) for u in job.frontier)


def test_frontier_add_is_idempotent():
    """Double-queueing one URL keeps a single slot (no stat/queue inflation)."""
    job = _job(_urls(4))
    _frontier_add(job, "http://example.com/new")
    _frontier_add(job, "http://example.com/new")
    assert job.frontier.count("http://example.com/new") == 1
    assert "http://example.com/new" in _frontier_set(job)


def test_reloaded_job_continues_past_visited_prefix():
    """A store reload (fresh object, transient head lost) resumes FIFO order."""
    urls = _urls(2 * BATCH_SIZE)
    first = CrawlJobState(job_id="j", frontier=list(urls), visited=set())
    batch1 = _pop_frontier(first)
    # Simulate the commit + a store round-trip: visited persisted, head lost.
    reloaded = CrawlJobState(job_id="j", frontier=list(urls), visited=set(batch1))
    batch2 = _pop_frontier(reloaded)
    assert batch2 == urls[BATCH_SIZE : 2 * BATCH_SIZE]
    assert not (set(batch1) & set(batch2))


async def test_completed_job_persists_empty_frontier(tmp_path):
    """COMPLETED jobs persist frontier == [] (stale offset slots reclaimed)."""
    import asyncio

    from app.api import job_runner
    from app.crawl.job_store import JobStatus, JobStore

    store = JobStore(tmp_path / "jobs.db")
    job = store.create("http://example.test/a", {})

    async def fake_execute(payload, **kwargs):
        return {
            "results": [
                {
                    "original_url": url,
                    "normalized_url": url,
                    "final_url": url,
                    "status": "success",
                    "classification": "rendered",
                    "discovered_links": [],
                }
                for url in payload["urls"]
            ]
        }

    job_runner.install_execute_crawl(fake_execute)
    try:
        await job_runner._run_job(job.job_id, store, object(), asyncio.Event())
    finally:
        job_runner.reset_execute_crawl()
    done = store.load(job.job_id)
    assert done is not None
    assert done.status == JobStatus.COMPLETED
    assert done.frontier == []
    assert done.visited == {"http://example.test/a"}
