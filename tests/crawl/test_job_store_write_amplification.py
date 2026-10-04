"""The durable job row must not carry the page history.

`JobStore._persist` used to write `results`, `visited` and `frontier` as JSON
columns on the single `crawl_jobs` row, so every batch persist re-serialized
the job's entire history. A 3000-page job therefore wrote roughly 9 MB of JSON
per batch, and total write volume over a crawl was quadratic in page count --
the store was doing O(n) of work per batch to record O(1) of new work.

The accumulators are monotonic: pages are appended to `results` and to
`visited`, and never rewritten. So they belong in append-only tables where a
batch costs one INSERT per new page instead of a rewrite of everything before
it. `frontier` stays a column -- it is a mutable queue, not an accumulator --
and the tests below pin the measured split rather than assuming it.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from app.crawl.job_store import CrawlJobState, JobStore
from app.models.crawl_policy import CrawlPolicy
from tests._job_harnesses import make_crawled_job_store, make_job_store
from tests._result_builders import _admit

_BODY = "x" * 2000


@pytest.fixture
def job_db(tmp_path):
    """The store under test and the file it lives in.

    Both come from ``make_job_store``, so this file measures the same store
    every other job test builds, and ``db_path`` is read back off the store
    rather than re-assembled from ``tmp_path`` -- a measurement of a file the
    store is not using would report zeros and pass.
    """
    store = make_job_store(str(tmp_path))
    return store, store.db_path


class TestConcurrentAccessHoldsTheConnectionLock:
    """Every use of the shared connection must happen under ``self._lock``.

    ``sqlite3`` hands one connection to several threads safely only when the
    caller serializes it. ``load`` fetched its row under the lock and then
    deserialized OUTSIDE it -- harmless while the page history came back in
    that same row, but moving the history into two child tables turned
    deserializing into two further queries on the shared connection with
    nothing held. Concurrent loads then drove one connection from two threads
    and it failed with ``InterfaceError: bad parameter or other API misuse``.

    That surfaced as a runner task dying mid-crawl and the job wedging with no
    write ever arriving, rather than as anything resembling a locking bug --
    which is why it needs a test that says so directly.
    """

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

    Both measurements below must read the real on-disk state rather than
    whatever the store happens to be holding, and neither may borrow the
    store's own connection: the test above is about what happens when two
    threads reach that one. The open/close pair is owned here so every reader
    gets the same separation instead of restating it.
    """
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _accumulated_bytes(db_path, job_id: str) -> int:
    """Bytes the job row itself spends, across every column it declares.

    The column list is taken from the live table rather than hardcoded, so this
    keeps measuring the row after the schema changes and cannot quietly go on
    measuring a column that is no longer there.
    """
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

    The shared ``make_crawled_job_store`` seeds history without bodies, which
    is the right seed for a test about how many pages a read returns. The byte
    measurement below is the opposite case: it is only meaningful if each page
    *would* have cost something to re-serialize, so the body is what the seed
    exists to carry.
    """
    job = store.create("https://example.com/", CrawlPolicy())
    for i in range(count):
        url = f"https://example.com/p{i}"
        job.results.append({"url": url, "status": "success", "body": _BODY})
        job.visited.add(_admit(url))
    store.save(job)
    return job


def test_the_job_row_does_not_grow_with_the_page_count(job_db):
    """Ten times the pages must not mean ten times the bytes per batch persist.

    This is the whole defect. Before the fix the row carried the history, so
    the second measurement here was ~5x the first; after it the row carries only
    live state and the two numbers are the same.
    """
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

    A row count that tracks the page count exactly is what makes the previous
    test mean something: if the history were also being mirrored somewhere else,
    the row would be flat for the wrong reason.
    """
    store, db_path = job_db

    job = _job_with_pages(store, 120)

    assert _result_rows(db_path, job.job_id) == 120


def test_a_second_save_of_an_unchanged_job_adds_no_rows(job_db):
    """Re-persisting without new pages must not duplicate the history.

    The batch loop saves on every iteration, including iterations that discover
    nothing. If save were a blind re-insert the table would grow without bound
    and a resumed job would replay pages it had already committed.
    """
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
