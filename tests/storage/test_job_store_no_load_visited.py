"""No-load visited saves: the post-growth tie-break reads the table fresh, then
an unchanged resave writes nothing to the visited table."""

from __future__ import annotations

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import OrderedUrlSet
from tests._policy_factories import canonical


def _touches_visited(stmt: str) -> bool:
    """True when ``stmt`` reads or writes the visited table."""
    return "crawl_job_visited" in stmt


def _visited_writes(stmts: list[str]) -> list[str]:
    """The visited-table INSERT/DELETE statements among ``stmts``."""
    return [
        stmt
        for stmt in stmts
        if _touches_visited(stmt) and stmt.lstrip().upper().startswith(("INSERT", "DELETE"))
    ]


def _replace_visited_and_expect(store, root: str, initial: list[str], replacement: list[str]) -> None:
    """Persist ``initial`` visited, swap to ``replacement``, save again with no
    load, then assert the reloaded set is exactly ``replacement``."""
    job = store.create(root, CrawlPolicy())
    job.visited = OrderedUrlSet(canonical(url) for url in initial)
    store.save(job)
    job.visited = OrderedUrlSet(canonical(url) for url in replacement)
    store.save(job)
    loaded = store.load(job.job_id)
    assert loaded is not None
    expected = OrderedUrlSet(canonical(url) for url in replacement)
    assert loaded.visited == expected
    assert len(loaded.visited) == len(replacement)


def test_visited_no_load_resave_is_read_free_no_write(job_store):
    """Growth-save visited, then resave unchanged: the fresh tie-break read runs
    once and no visited INSERT/DELETE follows (read-free, write-free)."""
    store = job_store
    job = store.create("http://no-load-visited-resave.test/", CrawlPolicy())
    job.visited = OrderedUrlSet(
        [
            canonical("http://no-load-visited-resave.test/alpha"),
            canonical("http://no-load-visited-resave.test/beta"),
        ]
    )
    store.save(job)

    events: list[str] = []

    def _trace(stmt: str) -> None:
        events.append(stmt)

    store._conn.set_trace_callback(_trace)

    store.save(job)  # no visited change: fresh read only, no writes

    assert not _visited_writes(events), f"an unchanged resave wrote to the visited table: {events!r}"
    reads = [stmt for stmt in events if _touches_visited(stmt) and stmt.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1, f"expected exactly the fresh tie-break read: {reads!r}"


def test_visited_no_load_same_length_replacement_persists(job_store):
    """Same-length visited replacement persists without a prior load(): the
    post-growth ``stored[2] is None`` branch detects the change on a fresh read."""
    _replace_visited_and_expect(
        job_store,
        "http://same-length-visited-no-load.test/",
        ["http://same-length-visited-no-load.test/a"],
        ["http://same-length-visited-no-load.test/b"],
    )


def test_visited_shrink_without_load_persists(job_store):
    """Shrunk visited set persists without a prior load(): the table heals to the
    shrunken set."""
    _replace_visited_and_expect(
        job_store,
        "http://shrink-visited-no-load.test/",
        [
            "http://shrink-visited-no-load.test/alpha",
            "http://shrink-visited-no-load.test/beta",
        ],
        ["http://shrink-visited-no-load.test/alpha"],
    )
