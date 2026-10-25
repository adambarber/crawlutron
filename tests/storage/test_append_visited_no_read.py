"""#519 item 2: ``_append_visited`` persists without reading the stored set."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from app.models.crawl_policy import CrawlPolicy
from app.models.normalized_url import NormalizedUrl
from app.storage.job_store import JobStore
from tests._policy_factories import canonical
from tests._policy_factories import job_store as store_on


def _crawled_job(store: JobStore, count: int):
    """A job whose visited set is built from one result row per crawled page."""
    job = store.create("https://example.com/", CrawlPolicy())
    pages = [{"url": url, "status": "success"} for url in _page_urls(count)]
    job.results.extend(pages)
    job.visited.update(canonical(page["url"]) for page in pages)
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

    sqlite3.connect = connect  # type: ignore[method-assign]
    try:
        return store_on(tmp_path / "jobs.db")
    finally:
        sqlite3.connect = real_connect  # type: ignore[method-assign]


def _on_disk_visited(db_path, job_id: str | None = None) -> list[str]:
    """The stored visited keys, read off a fresh connection to the file."""

    with sqlite3.connect(db_path) as conn:
        if job_id is None:
            return [row[0] for row in conn.execute('SELECT url FROM "crawl_job_visited"')]
        return [
            row[0] for row in conn.execute('SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
        ]


def test_persisting_overlapping_batches_never_selects_the_stored_visited_set(tmp_path):
    """A batch persist must run zero reads against the visited table.

    ``INSERT OR IGNORE`` over (job_id, url) is idempotent by construction, so
    the old pre-read's set difference computed nothing the statement did not."""
    statements: list[str] = []
    store = _store_with_traced_connection(tmp_path, statements)
    job = _crawled_job(store, 20)
    store.save(job)

    statements.clear()
    job.visited.add(canonical("https://example.com/p0"))  # overlapping member
    job.visited.add(canonical("https://example.com/fresh"))
    store.save(job)
    store.save(job)  # resave with nothing new

    visited_reads = [
        s for s in statements if "crawl_job_visited" in s.lower() and s.lstrip().upper().startswith("SELECT")
    ]
    assert visited_reads == [], f"the persist re-read the stored visited set: {visited_reads}"


def test_overlapping_visited_members_round_trip_without_duplicate_rows(tmp_path):
    """Trusting the insert alone must still produce exactly one row per URL.

    With the pre-read dropped, a batch whose members overlap the stored set
    must neither duplicate nor lose a page across two saves."""
    store = store_on(tmp_path / "jobs.db")
    job = _crawled_job(store, 10)

    overlap = canonical("https://example.com/p0")
    assert overlap in job.visited
    job.visited.update([overlap, canonical("https://example.com/p1")])
    job.visited.add(canonical("https://example.com/extra"))
    store.save(job)
    store.save(job)

    rows = _on_disk_visited(store.db_path, job.job_id)

    assert len(rows) == len(set(rows)), "duplicate visited rows landed"
    assert len(rows) == 11


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
