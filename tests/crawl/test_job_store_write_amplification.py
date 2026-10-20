"""The durable job row must not carry the page history.

``JobStore._persist`` used to write the history as JSON columns on one row
(quadratic re-serialization); the accumulators moved to child tables."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from app.models.crawl_policy import CrawlPolicy
from app.storage.job_store import CrawlJobState, JobStore
from tests._job_harnesses import make_crawled_job_store, make_job_store
from tests._policy_factories import canonical

_BODY = "x" * 2000


@pytest.fixture
def job_db(tmp_path):
    """The store under test and the file it lives in.

    Both come from ``make_job_store`` and ``db_path`` is read off the store,
    so a measurement of a file the store is not using cannot pass."""
    store = make_job_store(str(tmp_path))
    return store, store.db_path


class TestConcurrentAccessHoldsTheConnectionLock:
    """Every use of the shared connection must happen under ``self._lock``.

    ``load`` once deserialized OUTSIDE the lock; the child-table move turned
    that into unlocked queries and concurrent loads hit ``InterfaceError``."""

    _PAGES = 50
    _READERS = 8
    _ITERATIONS = 200

    @pytest.fixture
    def stored(self, tmp_path):
        return make_crawled_job_store(tmp_path, self._PAGES)

    def test_readers_and_a_writer_never_share_the_connection_unlocked(self, stored):
        store, job = stored
        errors: list[Exception] = []

        def _read() -> None:
            try:
                for _ in range(self._ITERATIONS):
                    loaded = store.load(job.job_id)
                    if loaded is None or len(loaded.visited) != self._PAGES:
                        raise AssertionError(f"short or missing load: {loaded!r}")
            except Exception as exc:
                errors.append(exc)

        def _write() -> None:
            try:
                for i in range(self._ITERATIONS):
                    job.stats = {"total_crawled": i}
                    store.save(job)
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=self._READERS + 1) as pool:
            futures = [pool.submit(_read) for _ in range(self._READERS)]
            futures.append(pool.submit(_write))
            for future in futures:
                future.result()

        assert not errors, f"the shared connection was used from two threads at once: {errors[0]!r}"


@contextmanager
def _on_disk(db_path):
    """A second connection to the store's file, for measuring what it wrote.

    Both measurements read real on-disk state and neither borrows the store's
    own connection -- the test above is about two threads on that one."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _accumulated_bytes(db_path, job_id: str) -> int:
    """Bytes the job row itself spends, across every column it declares.

    The column list comes from the live table, so this keeps measuring the row
    after the schema changes instead of a column that is no longer there."""
    with _on_disk(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(crawl_jobs)")]
        total = " + ".join(f"length(COALESCE(\"{column}\", ''))" for column in columns)
        row = conn.execute(f"SELECT {total} FROM crawl_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row[0] or 0


def _result_rows(db_path, job_id: str) -> int:
    with _on_disk(db_path) as conn:
        return conn.execute("SELECT count(*) FROM crawl_job_results WHERE job_id = ?", (job_id,)).fetchone()[
            0
        ]


def _job_with_pages(store: JobStore, count: int) -> CrawlJobState:
    """A job whose every page carries a body big enough to weigh.

    The shared seed leaves bodies out (right for read-count tests); the byte
    measurement needs pages that would have cost something to re-serialize."""
    job = store.create("https://example.com/", CrawlPolicy())
    for i in range(count):
        url = f"https://example.com/p{i}"
        job.results.append({"url": url, "status": "success", "body": _BODY})
        job.visited.add(canonical(url))
    store.save(job)
    return job


def test_the_job_row_does_not_grow_with_the_page_count(job_db):
    """Ten times the pages must not mean ten times the bytes per batch persist.

    This is the whole defect: before the fix the second measurement was ~5x
    the first; after it the row carries only live state and the two match."""
    store, db_path = job_db

    small_job = _job_with_pages(store, 50)
    at_fifty = _accumulated_bytes(db_path, small_job.job_id)

    big_job = _job_with_pages(store, 500)
    at_five_hundred = _accumulated_bytes(db_path, big_job.job_id)

    assert at_fifty > 0
    assert at_five_hundred <= at_fifty * 2, (
        f"the job row grew with the page count: {at_fifty} -> {at_five_hundred} bytes"
    )


def test_each_page_becomes_exactly_one_append_only_row(job_db):
    """The cost of a batch is one INSERT per new page, not a rewrite.

    A row count tracking the page count exactly is what makes the previous
    test mean something -- flat-for-the-wrong-reason is excluded."""
    store, db_path = job_db

    job = _job_with_pages(store, 120)

    assert _result_rows(db_path, job.job_id) == 120


def test_a_second_save_of_an_unchanged_job_adds_no_rows(job_db):
    """Re-persisting without new pages must not duplicate the history.

    The batch loop saves even on iterations that discover nothing; a blind
    re-insert would grow the table without bound and replay committed pages."""
    store, db_path = job_db
    job = _job_with_pages(store, 30)

    store.save(job)
    store.save(job)

    assert _result_rows(db_path, job.job_id) == 30


def test_reading_a_job_back_reproduces_every_result_in_order(job_db):
    """Splitting the row across tables must not change what a read returns.

    The accumulated collections are joined in insertion order, so a resumed or
    listed job sees the same history in the same sequence it was crawled in.
    """
    store, _db_path = job_db
    job = _job_with_pages(store, 40)

    loaded = store.load(job.job_id)

    assert loaded is not None
    assert [r["url"] for r in loaded.results] == [r["url"] for r in job.results]
    assert [r["body"] for r in loaded.results] == [_BODY] * 40
    assert loaded.visited == job.visited


def test_deleting_a_job_takes_its_pages_with_it(job_db):
    """The new tables are not an orphan store.

    `delete` returning True for a missing job is asserted elsewhere; what
    matters here is that no page rows survive the row they belong to.
    """
    store, db_path = job_db
    job = _job_with_pages(store, 25)

    assert store.delete(job.job_id) is True

    assert _result_rows(db_path, job.job_id) == 0
    assert store.load(job.job_id) is None


@pytest.mark.parametrize("count", [0, 1, 2])
def test_a_job_with_few_pages_round_trips(job_db, count):
    """The degenerate shapes, where a bulk-insert fast path would be wrong."""
    store, _db_path = job_db
    job = _job_with_pages(store, count)

    loaded = store.load(job.job_id)

    assert loaded is not None
    assert len(loaded.results) == count


def test_a_batch_save_inserts_only_the_pages_it_added(job_db):
    """The append must iterate the delta, not the collection.

    ``_append_results`` read the high-water mark but walked ``enumerate(results)``
    from the front; the row count was right, so it went unnoticed until now."""
    store, _db_path = job_db
    job = _job_with_pages(store, 30)

    def page_rows():
        with sqlite3.connect(store.db_path) as conn:
            return conn.execute(
                'SELECT count(*) AS n FROM "crawl_job_results" WHERE job_id = ?', (job.job_id,)
            ).fetchone()[0]

    assert page_rows() == 30
    for i in range(5):
        job.results.append({"url": f"https://example.com/new{i}", "status": "success"})
    store.save(job)

    assert page_rows() == 35, "a five-page batch did not add exactly five rows"
    # A resave with nothing new must not re-append.
    store.save(job)
    assert page_rows() == 35, "a resave re-appended the history"


def test_a_batch_save_visits_only_the_pages_it_added(job_db):
    """The visited insert carries the new set, not the whole set.

    Re-inserting the whole set was harmless (``INSERT OR IGNORE`` makes the key
    the answer) but quadratic: every save handled every URL the job had seen."""
    store, _db_path = job_db
    job = _job_with_pages(store, 30)

    def visited_rows():
        with sqlite3.connect(store.db_path) as conn:
            return conn.execute(
                'SELECT count(*) AS n FROM "crawl_job_visited" WHERE job_id = ?', (job.job_id,)
            ).fetchone()[0]

    assert visited_rows() == 30
    for i in range(5):
        job.results.append({"url": f"https://example.com/fresh{i}", "status": "success"})
        job.visited.add(canonical(f"https://example.com/fresh{i}"))
    store.save(job)

    assert visited_rows() == 35, "a five-page batch did not add exactly five visited rows"
    store.save(job)
    assert visited_rows() == 35, "a resave re-inserted the visited set"
