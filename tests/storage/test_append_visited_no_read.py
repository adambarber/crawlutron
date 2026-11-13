"""Visited persistence: #519 removed the pre-read; #539 adds the write delta."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing

from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.storage.job_store import JobStore
from tests._policy_factories import canonical
from tests._policy_factories import job_store as store_on


def _crawled_job(store: JobStore, count: int):
    """A job whose visited set is built from one result row per crawled page."""
    job = store.create("https://example.com/", CrawlPolicy())
    pages: list[dict[str, JsonValue]] = [{"url": url, "status": "success"} for url in _page_urls(count)]
    job.results.extend(pages)
    for page in pages:
        url_text = page["url"]
        assert isinstance(url_text, str)
        job.visited.update({canonical(url_text): None})
    return job


def _page_urls(count: int) -> Iterator[str]:
    return (f"https://example.com/p{i}" for i in range(count))


def _store_with_traced_connection(tmp_path, trace_target: list[str]) -> JobStore:
    """A store whose connection reports every statement into ``trace_target``.

    Intercepting :func:`sqlite3.connect` at construction is the only door: the
    connection itself is the store's private seam, and tracing is per-connection.
    """

    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(trace_target.append)
        return conn

    sqlite3.connect = connect
    try:
        return store_on(tmp_path / "jobs.db")
    finally:
        sqlite3.connect = real_connect


def _on_disk_visited(db_path, job_id: str | None = None) -> list[str]:
    """The stored visited keys, read off a fresh connection to the file."""

    with closing(sqlite3.connect(db_path)) as conn:
        if job_id is None:
            return [row[0] for row in conn.execute('SELECT url FROM "crawl_job_visited"')]
        return [
            row[0] for row in conn.execute('SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
        ]


def _visited_statements(statements: list[str], verb: str) -> list[str]:
    """The traced statements of one verb against the visited table; the trace
    sees one entry per executemany row, so this count IS the wire count."""
    return [s for s in statements if "crawl_job_visited" in s.lower() and s.lstrip().upper().startswith(verb)]


def _traced_crawled_job(tmp_path, statements: list[str], pages: int, *, grown: tuple[str, ...] = ()):
    """A traced store whose job of ``pages`` pages is saved once; pass ``grown``
    to also perform the SECOND save over those urls, tracing only its cost."""
    store = _store_with_traced_connection(tmp_path, statements)
    job = _crawled_job(store, pages)
    store.save(job)
    if grown:
        statements.clear()
        job.visited.update({canonical(url): None for url in grown})
        store.save(job)
    return store, job


def test_persisting_overlapping_batches_never_selects_the_stored_visited_set(tmp_path):
    """A delta persist must not re-read the stored visited set per save: the high-water mark lives on the
    live state, so the write delta needs no read at all. The FIRST save is the exception.
    """
    statements: list[str] = []
    store, job = _traced_crawled_job(
        tmp_path, statements, 20, grown=("https://example.com/p0", "https://example.com/fresh")
    )
    statements.clear()  # the seed/grow reads belong to the first save; measure only the delta saves
    job.visited.update({canonical("https://example.com/delta"): None})  # growth: add one new row
    store.save(job)

    visited_reads = _visited_statements(statements, "SELECT")
    assert visited_reads == [], f"the persist re-read the stored visited set: {visited_reads}"


def test_overlapping_visited_members_round_trip_without_duplicate_rows(tmp_path):
    """Trusting the insert alone must still produce exactly one row per URL.

    With the pre-read dropped, a batch whose members overlap the stored set
    must neither duplicate nor lose a page across two saves."""
    store = store_on(tmp_path / "jobs.db")
    job = _crawled_job(store, 10)

    overlap = canonical("https://example.com/p0")
    assert overlap in job.visited
    job.visited.update({overlap: None, canonical("https://example.com/p1"): None})
    job.visited.add(canonical("https://example.com/extra"))
    store.save(job)
    store.save(job)

    rows = _on_disk_visited(store.db_path, job.job_id)

    assert len(rows) == len(set(rows)), "duplicate visited rows landed"
    assert len(rows) == 11


def test_second_save_inserts_only_the_new_visited_tail(tmp_path):
    """#539: a save must INSERT only the urls the table does not hold yet.

    The trace sees one statement per executemany row, so the count IS the
    wire count: a second save sends the new tail, not the whole set again."""
    statements: list[str] = []
    store, job = _traced_crawled_job(
        tmp_path, statements, 20, grown=("https://example.com/new-a", "https://example.com/new-b")
    )

    inserts = _visited_statements(statements, "INSERT")
    assert len(inserts) == 2, (
        f"the second save sent {len(inserts)} visited INSERTs for 2 new urls -- "
        "the whole set is still re-sent per save"
    )


def test_a_fresh_process_save_inserts_nothing_already_stored(tmp_path):
    """A newly loaded job's first save must be a no-op, not a re-send.

    Everything a load brought back is already persisted, so the first save of
    a resumed process sends nothing -- the delta survives the boundary."""
    statements: list[str] = []
    store_a = _store_with_traced_connection(tmp_path, statements)
    job = _crawled_job(store_a, 20)
    store_a.save(job)

    reload_statements: list[str] = []
    store_b = _store_with_traced_connection(store_a.db_path.parent, reload_statements)
    reloaded = store_b.load(job.job_id)
    assert reloaded is not None
    store_b.save(reloaded)

    inserts = _visited_statements(reload_statements, "INSERT")
    assert inserts == [], f"a save of a freshly loaded job re-INSERTed {len(inserts)} already-stored urls"


def test_shrinking_visited_resyncs_the_stored_rows(tmp_path):
    """A visited set that shrank below the stored mark (the retry-requeue path
    discards) must resync: the on-disk rows land exactly on the live set."""
    store = store_on(tmp_path / "jobs.db")
    job = _crawled_job(store, 10)
    store.save(job)

    discarded = canonical("https://example.com/p3")
    job.visited.discard(discarded)
    store.save(job)

    rows = _on_disk_visited(store.db_path, job.job_id)
    assert set(rows) == {url.raw for url in job.visited}, "the shrink was not resynced"
    assert len(rows) == 9


def test_the_sibling_page_guard_still_holds_on_the_shared_file(tmp_path):
    """#140's cross-process delta keeps its own guard; this pins the round trip.

    A second store on the same file must load what the first wrote, pre-read
    or no pre-read."""
    store_a = store_on(tmp_path / "jobs.db")
    job = _crawled_job(store_a, 5)
    store_a.save(job)
    store_b = store_on(store_a.db_path)

    reloaded = store_b.load(job.job_id)

    assert reloaded is not None
    assert reloaded.visited == job.visited


def test_normalized_urls_are_the_only_persisted_spelling(tmp_path):
    """The persisted keys stay the canonical raw text of admitted values."""
    store = store_on(tmp_path / "jobs.db")
    job = _crawled_job(store, 1)
    store.save(job)

    rows = _on_disk_visited(store.db_path)

    assert rows == [NormalizedUrl("https://example.com/p0", policy=canonical("https://x/").policy).raw]
