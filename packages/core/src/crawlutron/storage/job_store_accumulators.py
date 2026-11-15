"""One crawl job's accumulators: page history, visited set, frontier queue.

Each collection gets its own table so a batch is one INSERT per new row. The
table-trust seam lives here: a skip compares against what the TABLE holds.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import islice
from typing import TYPE_CHECKING

from crawlutron.models.json_value import JsonValue
from crawlutron.safety.json_safe import json_default
from crawlutron.storage.safe_db import in_txn, txn_read
from crawlutron.storage.sqlite_store import connection_data_version

from .job_store_collections import FrontierEntryDict

if TYPE_CHECKING:
    from .job_store_state import CrawlJobState

__all__ = ["JobAccumulators"]

# Full-table read SQL per collection for the post-growth no-op tie-break
# when stored full_digest is None (no prior load). Same item shape as live.
_FULL_TABLE_SQL = {
    "results": 'SELECT payload FROM "crawl_job_results" WHERE job_id = ? ORDER BY seq',
    "visited": 'SELECT url FROM "crawl_job_visited" WHERE job_id = ?',
    "frontier": 'SELECT url, depth FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos',
}


def _table_row_items(collection: str, row: sqlite3.Row) -> dict | str:
    """One DB row as the same item shape the live collection holds, so
    digest comparison across the table boundary is exact."""
    if collection == "results":
        return json.loads(row["payload"])
    if collection == "visited":
        return row["url"]
    return {"url": row["url"], "depth": row["depth"]}


def _last_item_digest(accumulators: JobAccumulators, items: list) -> str:
    """Content identity of the final row: a whole-list replacement is seen."""
    return accumulators._digest(items[-1]) if items else ""


def _unchanged_since_save(accumulators: JobAccumulators, job_id: str, collection: str, items: list) -> bool:
    """True when our own last save is still the table's truth and nothing grew."""
    if accumulators._own_version.get(job_id) != accumulators._data_version():
        return False
    shape = accumulators._saved_shape.get((job_id, collection))
    if shape is None or shape[0] != len(items):
        return False
    return shape[1] == _last_item_digest(accumulators, items)


def _remember_shape(accumulators: JobAccumulators, job_id: str, collection: str, items: list) -> None:
    accumulators._saved_shape[(job_id, collection)] = (
        len(items),
        _last_item_digest(accumulators, items),
    )


class JobAccumulators:
    """The growing part of a job: page history, visited set and frontier queue."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Table-trust digests: (mark, tail_digest, full_digest); re-seeded on
        # data_version change so skips never ride stale memory.
        self._content_digest: dict[str, dict[str, tuple[int, str, str | None]]] = {}
        self._seeded_version: dict[str, int] = {}
        self._table_rows: dict[str, list] = {}
        self._table_rows_job: str | None = None
        self._digest_work_bytes: int = 0
        # No-op proof: our post-commit data_version, plus the saved (len, tail) shape.
        self._own_version: dict[str, int] = {}
        self._saved_shape: dict[tuple[str, str], tuple[int, str]] = {}

    def _data_version(self) -> int:
        return connection_data_version(self._conn)

    def _note_table_truth(self, job_id: str, version: int) -> None:
        """Stamp ``job_id``'s digests as truth as of ``version`` (reads just ran)."""
        self._seeded_version[job_id] = version

    def _seed_version(self, job_id: str, version: int) -> None:
        """Record the version observed before our write (the no-op proof)."""
        self._own_version[job_id] = version

    def _seed_table_truth(self, job_id: str) -> None:
        """Re-read the tables' content when another connection may have written.

        Runs inside the caller's write transaction; reads join it via ``in_txn``.
        """
        version = self._data_version()
        if self._seeded_version.get(job_id) == version:
            return
        with in_txn(self._conn):
            self._table_rows = {
                "results": self._conn.execute(
                    'SELECT payload FROM "crawl_job_results" WHERE job_id = ? ORDER BY seq', (job_id,)
                ).fetchall(),
                "visited": self._conn.execute(
                    'SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
                ).fetchall(),
                "frontier": self._conn.execute(
                    'SELECT url, depth FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos',
                    (job_id,),
                ).fetchall(),
            }
        self._table_rows_job = job_id
        self._seeded_version[job_id] = version

    def _digest(self, payload) -> str:
        """A stable content identity for one persisted batch."""
        encoded = json.dumps(payload, sort_keys=True, default=json_default).encode()
        self._digest_work_bytes += len(encoded)
        return hashlib.sha256(encoded).hexdigest()

    def digest_work_bytes(self) -> int:
        """Bytes fed to _digest since construction (test hook for the O(batch) pin)."""
        return self._digest_work_bytes

    def _stored(self, job_id: str, collection: str) -> tuple[int, str, str | None] | None:
        """The stored composite, or None / a legacy bare digest (never matches)."""
        stored = self._content_digest.get(job_id, {}).get(collection)
        if not (isinstance(stored, tuple) and len(stored) == 3):
            return None
        return stored

    def _content_matches(self, job_id: str, collection: str, mark: int, tail_digest: str) -> bool:
        """Content-identity seam: true iff the stored (mark, tail_digest) matches."""
        stored = self._stored(job_id, collection)
        return stored is not None and stored[0] == mark and stored[1] == tail_digest

    def _content_full_matches(self, job_id: str, collection: str, full_digest: str) -> bool:
        """Full-digest tie-break for the tail-empty ambiguity."""
        stored = self._stored(job_id, collection)
        if stored is None:
            return False
        if stored[2] is not None:
            return stored[2] == full_digest
        with in_txn(self._conn):
            rows = self._conn.execute(_FULL_TABLE_SQL[collection], (job_id,)).fetchall()
        return self._digest([_table_row_items(collection, row) for row in rows]) == full_digest

    def _record_content(
        self,
        job_id: str,
        collection: str,
        mark: int,
        tail_digest: str,
        full_digest: str | None = None,
    ) -> None:
        self._content_digest.setdefault(job_id, {})[collection] = (mark, tail_digest, full_digest)

    def _tail_nop(self, job_id: str, collection: str, mark: int) -> None:
        """Record that the persisted tail already matches: no rows written."""
        self._record_content(job_id, collection, mark, self._digest([]), None)

    def read_results(self, job_id: str) -> list[dict[str, JsonValue]]:
        """The page history, in the order it was crawled."""
        with txn_read(self._conn, owner="job_store_accumulators.read_results"):
            rows = self._conn.execute(
                'SELECT payload FROM "crawl_job_results" WHERE job_id = ? ORDER BY seq', (job_id,)
            ).fetchall()
        payloads = [json.loads(row["payload"]) for row in rows]
        # The loaded page history is already persisted: remember its identity so a
        # fresh process's first save is a no-op instead of a resync.
        self._record_content(job_id, "results", len(payloads), self._digest([]), self._digest(payloads))
        return payloads

    def read_visited(self, job_id: str) -> list[str]:
        with txn_read(self._conn, owner="job_store_accumulators.read_visited"):
            rows = self._conn.execute(
                'SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
            ).fetchall()
        urls = [row["url"] for row in rows]
        # The loaded visited set is already persisted: remember its identity so a
        # fresh process's first save is a no-op instead of a resync.
        self._record_content(job_id, "visited", len(urls), self._digest([]), self._digest(urls))
        return urls

    def read_frontier(self, job_id: str) -> list[FrontierEntryDict]:
        """The outstanding queue, in queue order."""
        with txn_read(self._conn, owner="job_store_accumulators.read_frontier"):
            rows = self._conn.execute(
                'SELECT url, depth FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos', (job_id,)
            ).fetchall()
        frontier: list[FrontierEntryDict] = [{"url": row["url"], "depth": row["depth"]} for row in rows]
        # The loaded queue is already persisted: remember its identity so a fresh
        # process's first save is a no-op instead of a resync.
        self._record_content(job_id, "frontier", len(frontier), self._digest([]), self._digest(frontier))
        return frontier

    def append(self, job: CrawlJobState) -> None:
        self._append_results(job)
        self._append_visited(job)
        self._reconcile_frontier(job)

    def delete(self, job_id: str) -> None:
        """Drop a job's history."""
        with in_txn(self._conn):
            self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
            self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))
        self._content_digest.pop(job_id, None)
        self._seeded_version.pop(job_id, None)

    def counts_by_job(self) -> dict[str, tuple[int, int]]:
        """``{job_id: (visited, results)}`` in one grouped scan, not one query per job."""
        with txn_read(self._conn, owner="job_store_accumulators.counts_by_job"):
            rows = self._conn.execute(
                """
                SELECT job_id,
                       (SELECT count(*) FROM crawl_job_visited v WHERE v.job_id = j.job_id) AS visited_count,
                       (SELECT count(*) FROM crawl_job_results r WHERE r.job_id = j.job_id) AS results_count
                  FROM crawl_jobs j
                """
            ).fetchall()
        return {row["job_id"]: (row["visited_count"], row["results_count"]) for row in rows}

    def counts_for(self, job_id: str) -> tuple[int, int]:
        """``(visited, results)`` for one job without reading either list."""
        with txn_read(self._conn, owner="job_store_accumulators.counts_for"):
            row = self._conn.execute(
                "SELECT (SELECT count(*) FROM crawl_job_visited WHERE job_id = ?) AS visited,"
                " (SELECT count(*) FROM crawl_job_results WHERE job_id = ?) AS results",
                (job_id, job_id),
            ).fetchone()
        return row["visited"], row["results"]

    def _max_seq_plus_one(self, job_id: str) -> int:
        """The results high-water mark: one past the highest persisted ``seq``."""
        with in_txn(self._conn):
            row = self._conn.execute(
                'SELECT max(seq) FROM "crawl_job_results" WHERE job_id = ?', (job_id,)
            ).fetchone()
        return 0 if row["max(seq)"] is None else row["max(seq)"] + 1

    def _frontier_tail(self, job: CrawlJobState, mark: int) -> list[dict[str, object]]:
        """The queue tail beyond the persisted mark, as dicts for digesting."""
        return [{"url": entry.url.raw, "depth": entry.depth} for entry in islice(job.frontier, mark, None)]

    def _frontier_resync(self, job_id: str) -> None:
        """Drop the whole frontier; caller re-inserts from mark 0."""
        with in_txn(self._conn):
            self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))

    def _frontier_write_tail(self, job_id: str, mark: int, tail: list[dict[str, object]]) -> None:
        """Append the frontier tail after the table's current high-water pos."""
        with in_txn(self._conn):
            row = self._conn.execute(
                'SELECT max(pos) AS high FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,)
            ).fetchone()
            high = row["high"]
            start = 0 if high is None else high + 1
            self._conn.executemany(
                # OR REPLACE, not OR IGNORE: a re-inserted identity carries its new
                # depth and position -- the write edge's decision, not the table's.
                'INSERT OR REPLACE INTO "crawl_job_frontier" (job_id, url, pos, depth) VALUES (?, ?, ?, ?)',
                ((job_id, entry["url"], pos, entry["depth"]) for pos, entry in enumerate(tail, start=start)),
            )

    def _reconcile_frontier(self, job: CrawlJobState) -> None:
        """Reconcile the live queue against its table rows."""
        job_id = job.job_id
        if not job.frontier_removed and _unchanged_since_save(self, job_id, "frontier", list(job.frontier)):
            return
        with in_txn(self._conn):
            for url in job.frontier_removed:
                self._conn.execute(
                    'DELETE FROM "crawl_job_frontier" WHERE job_id = ? AND url = ?', (job_id, url.raw)
                )
        job.frontier_removed.clear()
        mark = job.frontier_mark

        skip_write = False
        if mark > len(job.frontier):
            # The live list shrank below the persisted prefix by a mutation that
            # logged no removal -- a wholesale replacement. Resync, O(queue).
            self._frontier_resync(job_id)
            mark = 0
        elif mark == len(job.frontier):
            # Tail empty: no-op and same-length replacement share mark + tail.
            full_digest = self._digest(
                [{"url": entry.url.raw, "depth": entry.depth} for entry in job.frontier]
            )
            if self._content_full_matches(job_id, "frontier", full_digest):
                # Content identical to the persisted table: nothing to write.
                job.frontier_mark = len(job.frontier)
                self._record_content(job_id, "frontier", len(job.frontier), self._digest([]), full_digest)
                _remember_shape(self, job_id, "frontier", list(job.frontier))
                return
            # Same-length replacement: resync the whole queue, byte-identical.
            self._frontier_resync(job_id)
            mark = 0
        elif self._content_matches(job_id, "frontier", mark, self._digest(self._frontier_tail(job, mark))):
            # Growth tail already persisted: nothing to write.
            skip_write = True
        if not skip_write:
            # tail is computed AFTER mark adjustment so a resync writes the whole queue.
            self._frontier_write_tail(job_id, mark, self._frontier_tail(job, mark))
        job.frontier_mark = len(job.frontier)
        self._record_content(job_id, "frontier", len(job.frontier), self._digest([]), None)
        _remember_shape(self, job_id, "frontier", list(job.frontier))

    def _append_results(self, job: CrawlJobState) -> None:
        """Insert only pages this batch added, keyed off the table's high-water mark."""
        job_id = job.job_id
        results = job.results
        if _unchanged_since_save(self, job_id, "results", results):
            return
        table = self._table_rows.get("results", []) if self._table_rows_job == job_id else []
        persisted = self._max_seq_plus_one(job_id)
        tail = results[persisted:]
        tail_digest = self._digest(tail)

        if persisted < len(results):
            # Growth: digest only the new tail (O(new rows)).
            if self._content_matches(job_id, "results", persisted, tail_digest):
                # No-op: the tail is already persisted. Keep the stored full digest.
                _remember_shape(self, job_id, "results", results)
                return
        else:
            # Ambiguous: tail empty -- no-op and same-length replacement look
            # identical to mark + tail; whole-collection digest tie-breaks.
            full_digest = self._digest(results)
            if self._content_full_matches(job_id, "results", full_digest):
                # No-op: the table already holds the live history.
                self._record_content(job_id, "results", len(results), self._digest([]), full_digest)
                _remember_shape(self, job_id, "results", results)
                return
            # Same-length replacement or shrink: fall through to resync below.
        if table and self._digest(results[: len(table)]) != self._digest(table):
            # Table head differs from live head (out-of-band change): resync whole.
            with in_txn(self._conn):
                self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            table = []
            persisted = 0
        if persisted >= len(results):
            # Shrank or same-length replacement: resync so the table matches exactly.
            with in_txn(self._conn):
                self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            persisted = 0
        with in_txn(self._conn):
            self._conn.executemany(
                'INSERT OR IGNORE INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
                (
                    (job_id, seq, _json(result))
                    for seq, result in enumerate(results[persisted:], start=persisted)
                ),
            )
        self._record_content(job_id, "results", len(results), self._digest([]), None)
        _remember_shape(self, job_id, "results", results)

    def _append_visited(self, job: CrawlJobState) -> None:
        """Insert only the visited tail not yet sent, keyed off the visited_mark."""
        job_id = job.job_id
        if _unchanged_since_save(self, job_id, "visited", list(job.visited)):
            return
        mark = job.visited_mark
        tail_urls = [url.raw for url in islice(job.visited, mark, None)]
        tail_digest = self._digest(tail_urls)

        skip_write = False
        if mark > len(job.visited):
            # The set shrank below the mark (retry-requeue discard) resyncs,
            # and INSERT OR IGNORE stays: the ignore is the cross-process guard.
            with in_txn(self._conn):
                self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
            job.visited_mark = 0
            mark = 0
        elif mark == len(job.visited):
            # Tail empty: no-op or same-length replacement → full-digest tie-break.
            full_digest = self._digest([url.raw for url in job.visited])
            if self._content_full_matches(job_id, "visited", full_digest):
                # Content identical to the persisted table: nothing to write.
                job.visited_mark = len(job.visited)
                self._record_content(job_id, "visited", len(job.visited), self._digest([]), full_digest)
                _remember_shape(self, job_id, "visited", list(job.visited))
                return
            # Same-length replacement: resync.
            with in_txn(self._conn):
                self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
            mark = 0
        elif self._content_matches(job_id, "visited", mark, tail_digest):
            # Growth tail already persisted: nothing to write.
            skip_write = True
        if not skip_write:
            with in_txn(self._conn):
                self._conn.executemany(
                    'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
                    ((job_id, url.raw) for url in islice(job.visited, mark, None)),
                )
        job.visited_mark = len(job.visited)
        self._record_content(job_id, "visited", len(job.visited), self._digest([]), None)
        _remember_shape(self, job_id, "visited", list(job.visited))


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=json_default)
