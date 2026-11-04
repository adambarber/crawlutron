"""One crawl job's accumulators: page history, visited set, frontier queue.

Each collection gets its own table so a batch is one INSERT per new row. The
table-trust seam lives here too: a skip decision compares in-memory content
against what the TABLE holds (re-seeded at save entry via PRAGMA data_version),
never process-local memory alone.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import islice
from typing import TYPE_CHECKING

from crawlutron.models.json_value import JsonValue
from crawlutron.safety.json_safe import json_default
from crawlutron.storage.sqlite_store import connection_data_version

from .job_store_collections import FrontierEntryDict

if TYPE_CHECKING:
    from .job_store_state import CrawlJobState

__all__ = ["JobAccumulators"]


class JobAccumulators:
    """The growing part of a job: its append-only page history, visited set and
    frontier queue, in their own tables so a batch is one INSERT per new row
    (row is batch-bounded: frontier pops per batch, in_flight by concurrency)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Table-trust digests: what the TABLE last held per (job_id, collection),
        # re-seeded at save entry whenever another connection has committed
        # (PRAGMA data_version), so a skip never rides a stale memory digest.
        self._content_digest: dict[str, dict[str, str]] = {}
        self._seeded_version: dict[str, int] = {}
        self._table_rows: dict[str, list] = {}
        self._table_rows_job: str | None = None

    def _data_version(self) -> int:
        return connection_data_version(self._conn)

    def _note_table_truth(self, job_id: str, version: int) -> None:
        """Stamp ``job_id``'s digests as truth as of ``version`` (reads just ran)."""
        self._seeded_version[job_id] = version

    def _seed_table_truth(self, job_id: str) -> None:
        """Re-read the tables' content when another connection may have written.

        A skip decision must compare against what the TABLE holds, not what this
        process last wrote; own commits leave ``data_version`` put, so the
        no-op path stays read-free. The version is read BEFORE the rows so a
        commit landing mid-read forces the next save to re-seed."""
        version = self._data_version()
        if self._seeded_version.get(job_id) == version:
            return
        self._table_rows = {
            "results": self.read_results(job_id),
            "visited": self.read_visited(job_id),
            "frontier": self.read_frontier(job_id),
        }
        self._table_rows_job = job_id
        self._seeded_version[job_id] = version

    def _digest(self, payload) -> str:
        """A stable content identity for one persisted batch."""
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=json_default).encode()).hexdigest()

    def _content_matches(self, job_id: str, collection: str, digest: str) -> bool:
        """The single content-identity seam every accumulator mutation gates on.

        True iff ``digest`` matches the TABLE's persisted content -- the map is
        re-seeded from the tables at each save entry -- so an identical batch is
        not re-written and an out-of-band table change is never skipped over.
        """
        return self._content_digest.get(job_id, {}).get(collection) == digest

    def _record_content(self, job_id: str, collection: str, digest: str) -> None:
        self._content_digest.setdefault(job_id, {})[collection] = digest

    def read_results(self, job_id: str) -> list[dict[str, JsonValue]]:
        """The page history, in the order it was crawled.

        Each payload is the response model's own dump (the write edge), so the
        declared read is the dump's shape, not ``Any``.
        """
        rows = self._conn.execute(
            'SELECT payload FROM "crawl_job_results" WHERE job_id = ? ORDER BY seq', (job_id,)
        ).fetchall()
        payloads = [json.loads(row["payload"]) for row in rows]
        # The loaded page history is already persisted: remember its digest so a
        # fresh process's first save is a no-op instead of a resync.
        self._record_content(job_id, "results", self._digest(payloads))
        return payloads

    def read_visited(self, job_id: str) -> list[str]:
        rows = self._conn.execute(
            'SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
        ).fetchall()
        urls = [row["url"] for row in rows]
        # The loaded visited set is already persisted: remember its digest so a
        # fresh process's first save is a no-op instead of a resync.
        self._record_content(job_id, "visited", self._digest(urls))
        return urls

    def read_frontier(self, job_id: str) -> list[FrontierEntryDict]:
        """The outstanding queue, in queue order.

        Rows are keyed by URL, so a read is every entry the job still holds;
        the identity merge on read filters what admission no longer keeps.
        """
        rows = self._conn.execute(
            'SELECT url, depth FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos', (job_id,)
        ).fetchall()
        frontier: list[FrontierEntryDict] = [{"url": row["url"], "depth": row["depth"]} for row in rows]
        # The loaded queue is already persisted: remember its digest so a fresh
        # process's first save is a no-op instead of a resync.
        self._record_content(job_id, "frontier", self._digest(frontier))
        return frontier

    def append(self, job: CrawlJobState) -> None:
        self._append_results(job)
        self._append_visited(job)
        self._reconcile_frontier(job)

    def delete(self, job_id: str) -> None:
        """Drop a job's history.
        Without this, the tables would keep a deleted job's crawl with nothing
        pointing at it -- the old column layout deleted the history by construction.
        """
        self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
        self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
        self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))
        # Drop the remembered content identity so a re-created job with the same
        # job_id is persisted in full instead of matching a tombstoned digest.
        self._content_digest.pop(job_id, None)
        self._seeded_version.pop(job_id, None)

    def counts_by_job(self) -> dict[str, tuple[int, int]]:
        """``{job_id: (visited, results)}`` for the job list, in one query, instead of a
        correlated subquery round trip per job per counter -- the primary keys make
        both counts a single grouped scan of the two tables.
        """
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
        """``(visited, results)`` for one job, without reading either list: ``count(*)``
        over the primary key is an index probe, giving ``len()``'s answer for none
        of the load, so a status read reports a size without materializing the history.
        """
        row = self._conn.execute(
            "SELECT (SELECT count(*) FROM crawl_job_visited WHERE job_id = ?) AS visited,"
            " (SELECT count(*) FROM crawl_job_results WHERE job_id = ?) AS results",
            (job_id, job_id),
        ).fetchone()
        return row["visited"], row["results"]

    def _reconcile_frontier(self, job: CrawlJobState) -> None:
        """Reconcile the live queue against its table rows.

        ``frontier_removed`` names rows to delete, ``frontier_mark`` splits the
        persisted prefix from the appended tail: a batch is O(removed+appended).
        A same-length replacement is detected by content identity, not by the
        persisted prefix length, so it is never silently skipped.
        """
        job_id = job.job_id
        for url in job.frontier_removed:
            self._conn.execute(
                'DELETE FROM "crawl_job_frontier" WHERE job_id = ? AND url = ?', (job_id, url.raw)
            )
        job.frontier_removed.clear()
        digest = self._digest([{"url": entry.url.raw, "depth": entry.depth} for entry in job.frontier])
        mark = job.frontier_mark
        if self._content_matches(job_id, "frontier", digest):
            # Content identical to the persisted table: nothing to write.
            job.frontier_mark = len(job.frontier)
            return
        if mark > len(job.frontier):
            # The live list shrank below the persisted prefix by a mutation that
            # logged no removal -- a wholesale replacement. Resync, O(queue).
            self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))
            mark = 0
        if mark == len(job.frontier):
            # Same-length replacement: the tail is empty but the content changed,
            # so the table still holds the old rows. Resync the whole queue.
            self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))
            mark = 0
        row = self._conn.execute(
            'SELECT max(pos) AS high FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,)
        ).fetchone()
        high = row["high"]
        start = 0 if high is None else high + 1
        self._conn.executemany(
            # OR REPLACE, not OR IGNORE: a re-inserted identity carries its new
            # depth and position -- the write edge's decision, not the table's.
            'INSERT OR REPLACE INTO "crawl_job_frontier" (job_id, url, pos, depth) VALUES (?, ?, ?, ?)',
            (
                (job_id, entry.url.raw, pos, entry.depth)
                for pos, entry in enumerate(islice(job.frontier, mark, None), start=start)
            ),
        )
        job.frontier_mark = len(job.frontier)
        self._record_content(job_id, "frontier", digest)

    def _append_results(self, job: CrawlJobState) -> None:
        """Insert only the pages this batch added, keyed off the table's high-water mark
        (not a remembered count, so a resumed process still appends exactly what it
        hasn't written). If normalization shrank history, resync rather than silently skip.
        A same-length replacement is detected by content identity and resynced.
        """
        job_id = job.job_id
        results = job.results
        digest = self._digest(results)
        if self._content_matches(job_id, "results", digest):
            return
        table = self._table_rows.get("results", []) if self._table_rows_job == job_id else []
        if self._digest(results[: len(table)]) != self._digest(table):
            # The stored rows are not the live history's head (an out-of-band
            # change): the tail delta cannot splice, so resync in full.
            self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            table = []
        row = self._conn.execute(
            'SELECT max(seq) FROM "crawl_job_results" WHERE job_id = ?', (job_id,)
        ).fetchone()
        persisted = 0 if row["max(seq)"] is None else row["max(seq)"] + 1
        if persisted >= len(results):
            # Shrank or same-length replacement: the table may hold stale rows,
            # so resync so the table matches the live content exactly.
            self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            persisted = 0
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
            ((job_id, seq, _json(result)) for seq, result in enumerate(results[persisted:], start=persisted)),
        )
        self._record_content(job_id, "results", digest)

    def _append_visited(self, job: CrawlJobState) -> None:
        """Insert only the visited tail this state has not sent yet, keyed off
        the live ``visited_mark`` (the ``_append_results`` pattern): the set is
        insertion-ordered, so the slice after the mark is exactly what is new.
        A same-length replacement is detected by content identity and resynced.
        """
        job_id = job.job_id
        digest = self._digest([url.raw for url in job.visited])
        if self._content_matches(job_id, "visited", digest):
            # Content identical to the persisted table: nothing to write.
            job.visited_mark = len(job.visited)
            return
        if job.visited_mark > len(job.visited):
            # A set that shrank below the mark (retry-requeue discard) resyncs,
            # and INSERT OR IGNORE stays: the ignore is the cross-process guard.
            self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
            job.visited_mark = 0
        mark = job.visited_mark
        if mark == len(job.visited):
            # Same-length replacement: the tail is empty but the content changed,
            # so the table still holds the old rows. Resync the whole set.
            self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
            mark = 0
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
            ((job_id, url.raw) for url in islice(job.visited, mark, None)),
        )
        job.visited_mark = len(job.visited)
        self._record_content(job_id, "visited", digest)


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=json_default)
