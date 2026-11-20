"""In_txn is load-bearing for every JobAccumulators write path.

Calling any DML method outside a write transaction must raise WriteRejected.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store_accumulators import JobAccumulators
from crawlutron.storage.job_store_state import CrawlJobState
from crawlutron.storage.safe_db import WriteRejected
from tests._policy_factories import canonical


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE crawl_job_results (job_id TEXT, seq INTEGER, payload TEXT, PRIMARY KEY (job_id, seq))"
    )
    conn.execute("CREATE TABLE crawl_job_visited (job_id TEXT, url TEXT, PRIMARY KEY (job_id, url))")
    conn.execute('CREATE TABLE "crawl_jobs" (job_id TEXT PRIMARY KEY, url TEXT, policy TEXT)')
    conn.execute(
        'CREATE TABLE "crawl_job_frontier" '
        "(job_id TEXT, url TEXT, pos INTEGER, depth INTEGER, "
        "PRIMARY KEY (job_id, url))"
    )
    return conn


@contextmanager
def _accumulators() -> Generator[tuple[JobAccumulators, sqlite3.Connection]]:
    """A JobAccumulators over a fresh in-memory schema, closed on exit."""
    conn = _make_conn()
    try:
        yield JobAccumulators(conn), conn
    finally:
        conn.close()


def _empty_job() -> CrawlJobState:
    """A bare CrawlJobState with empty collections."""
    return CrawlJobState(job_id="job-1", policy=CrawlPolicy())


def _refuses(acc: JobAccumulators, method: str, *args: object) -> None:
    """The named write method raises WriteRejected with no open transaction."""
    with pytest.raises(WriteRejected, match="outside"):
        acc.__getattribute__(method)(*args)


def test_delete_raises_outside_txn() -> None:
    with _accumulators() as (acc, _conn):
        _refuses(acc, "delete", "job-1")


def test_append_results_raises_outside_txn() -> None:
    with _accumulators() as (acc, conn):
        conn.execute(
            'INSERT OR IGNORE INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
            ("job-1", 0, '{"url":"http://a.test/0"}'),
        )
        job = _empty_job()
        job.results.append({"url": "http://a.test/1"})
        _refuses(acc, "_append_results", job)


def test_append_visited_raises_outside_txn() -> None:
    with _accumulators() as (acc, _conn):
        job = _empty_job()
        job.visited.add(canonical("http://a.test/"))
        _refuses(acc, "_append_visited", job)


def test_frontier_resync_raises_outside_txn() -> None:
    with _accumulators() as (acc, _conn):
        _refuses(acc, "_frontier_resync", "job-1")


def test_frontier_write_tail_raises_outside_txn() -> None:
    with _accumulators() as (acc, _conn):
        _refuses(acc, "_frontier_write_tail", "job-1", 0, [{"url": "http://a.test/", "depth": 0}])


def test_reconcile_frontier_raises_outside_txn() -> None:
    with _accumulators() as (acc, _conn):
        job = _empty_job()
        job.frontier_removed.append(canonical("http://a.test/"))
        _refuses(acc, "_reconcile_frontier", job)
