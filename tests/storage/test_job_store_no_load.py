"""Same-length replacement and shrink without a prior load(): stored[2] is None
after a growth write, so the post-growth tie-break must read the table fresh."""

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import FrontierEntry, OrderedUrlSet
from tests._job_harnesses import frontier, seed_frontier
from tests._policy_factories import canonical


def test_same_length_results_replacement_without_load_is_persisted(job_store):
    """Same-length results replacement persists without a prior load(): the
    post-growth ``stored[2] is None`` branch detects content change via a fresh
    table read, not skip on mark equality alone."""
    job = job_store.create("http://same-length-results.test/", CrawlPolicy())
    job.results = [{"url": "http://same-length-results.test/a", "status": "ok"}]
    job_store.save(job)

    job.results = [{"url": "http://same-length-results.test/b", "status": "ok"}]
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # same-length results reload
    assert loaded is not None
    assert len(loaded.results) == 1
    assert loaded.results[0]["url"] == "http://same-length-results.test/b"
    assert loaded.results[0]["status"] == "ok"


def test_same_length_visited_replacement_without_load_is_persisted(job_store):
    """Same-length visited replacement persists without a prior load()."""
    job = job_store.create("http://same-length-visited.test/", CrawlPolicy())
    job.visited = OrderedUrlSet([canonical("http://same-length-visited.test/a")])
    job_store.save(job)

    job.visited = OrderedUrlSet([canonical("http://same-length-visited.test/b")])
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # same-length visited reload
    assert loaded is not None
    assert loaded.visited == OrderedUrlSet([canonical("http://same-length-visited.test/b")])
    assert len(loaded.visited) == 1


def test_same_length_frontier_replacement_without_load_is_persisted(job_store):
    """Same-length frontier replacement persists without a prior load()."""
    job = job_store.create("http://same-length-frontier.test/", CrawlPolicy())
    seed_frontier(job, frontier(["http://same-length-frontier.test/page2"]))
    job_store.save(job)

    job.frontier = [FrontierEntry(url=canonical("http://same-length-frontier.test/page3"), depth=0)]
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # same-length frontier reload
    assert loaded is not None
    loaded_frontier = [FrontierEntry(url=canonical("http://same-length-frontier.test/page3"), depth=0)]
    assert loaded.frontier == loaded_frontier
    assert loaded.frontier[0].depth == 0


def test_shrink_results_without_load_is_persisted(job_store):
    """Shrunk results history persists without a prior load(): the table holds more
    rows than live history, so the persist must resync to the shrunken set."""
    job = job_store.create("http://shrink-results.test/", CrawlPolicy())
    job.results = [
        {"url": "http://shrink-results.test/a", "status": "ok"},
        {"url": "http://shrink-results.test/b", "status": "ok"},
    ]
    job_store.save(job)

    job.results = [{"url": "http://shrink-results.test/a", "status": "ok"}]
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # shrink results reload
    assert loaded is not None
    assert len(loaded.results) == 1
    assert loaded.results[0]["url"] == "http://shrink-results.test/a"
    assert loaded.results[0]["status"] == "ok"


def test_shrink_visited_without_load_is_persisted(job_store):
    """Shrunk visited set persists without a prior load()."""
    job = job_store.create("http://shrink-visited.test/", CrawlPolicy())
    job.visited = OrderedUrlSet(
        [canonical("http://shrink-visited.test/a"), canonical("http://shrink-visited.test/b")]
    )
    job_store.save(job)

    job.visited = OrderedUrlSet([canonical("http://shrink-visited.test/a")])
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # shrink visited reload
    assert loaded is not None
    assert loaded.visited == OrderedUrlSet([canonical("http://shrink-visited.test/a")])
    assert len(loaded.visited) == 1


def test_shrink_frontier_without_load_is_persisted(job_store):
    """Shrunk frontier persists without a prior load()."""
    job = job_store.create("http://shrink-frontier.test/", CrawlPolicy())
    seed_frontier(
        job,
        frontier(["http://shrink-frontier.test/page2", "http://shrink-frontier.test/page3"]),
    )
    job_store.save(job)

    job.frontier = [FrontierEntry(url=canonical("http://shrink-frontier.test/page2"), depth=0)]
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # shrink frontier reload
    assert loaded is not None
    loaded_frontier = [FrontierEntry(url=canonical("http://shrink-frontier.test/page2"), depth=0)]
    assert loaded.frontier == loaded_frontier
    assert loaded.frontier[0].depth == 0
