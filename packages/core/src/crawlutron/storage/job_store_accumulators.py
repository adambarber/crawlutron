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
        # Table-trust digests: (mark, tail_digest, full_digest); re-seeded on
        # data_version change so skips never ride stale memory.
        self._content_digest: dict[str, dict[str, tuple[int, str, str | None]]] = {}
        self._seeded_version: dict[str, int] = {}
        self._table_rows: dict[str, list] = {}
        self._table_rows_job: str | None = None
        self._digest_work_bytes: int = 0

    def _data_version(self) -> int:
        return connection_data_version(self._conn)

    def _note_table_truth(self, job_id: str, version: int) -> None:
        """Stamp ``job_id``'s digests as truth as of ``version`` (reads just ran)."""
        self._seeded_version[job_id] = version

    def _seed_table_truth(self, job_id: str) -> None:
        """Re-read the tables' content when another connection may have written.

        A skip must compare against the TABLE, not this process's last write; own
        commits leave ``data_version`` put, so the no-op path stays read-free.
        """
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
        """Content-identity seam: true iff the stored (mark, tail_digest) matches.
        A legacy bare digest never matches, so the caller falls through to reconcile.
        """
        stored = self._stored(job_id, collection)
        return stored is not None and stored[0] == mark and stored[1] == tail_digest

    def _content_full_matches(self, job_id: str, collection: str, full_digest: str) -> bool:
        """Full-digest tie-break for the ambiguous tail-empty case (mark == length).
        Separates a no-op from a same-length replacement; keeps resync byte-identical.
        """
        stored = self._stored(job_id, collection)
        return stored is not None and stored[2] is not None and stored[2] == full_digest

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
        """The page history, in the order it was crawled.

        Each payload is the response model's own dump (the write edge), so the
        declared read is the dump's shape, not ``Any``.
        """
        rows = self._conn.execute(
            'SELECT payload FROM "crawl_job_results" WHERE job_id = ? ORDER BY seq', (job_id,)
        ).fetchall()
        payloads = [json.loads(row["payload"]) for row in rows]
        # The loaded page history is already persisted: remember its identity so a
        # fresh process's first save is a no-op instead of a resync.
        self._record_content(job_id, "results", len(payloads), self._digest([]), self._digest(payloads))
        return payloads

    def read_visited(self, job_id: str) -> list[str]:
        rows = self._conn.execute(
            'SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
        ).fetchall()
        urls = [row["url"] for row in rows]
        # The loaded visited set is already persisted: remember its identity so a
        # fresh process's first save is a no-op instead of a resync.
        self._record_content(job_id, "visited", len(urls), self._digest([]), self._digest(urls))
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
        # The loaded queue is already persisted: remember its identity so a fresh
        # process's first save is a no-op instead of a resync.
        self._record_content(job_id, "frontier", len(frontier), self._digest([]), self._digest(frontier))
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

    def _max_seq_plus_one(self, job_id: str) -> int:
        """The results high-water mark: one past the highest persisted ``seq``."""
        row = self._conn.execute(
            'SELECT max(seq) FROM "crawl_job_results" WHERE job_id = ?', (job_id,)
        ).fetchone()
        return 0 if row["max(seq)"] is None else row["max(seq)"] + 1

    def _frontier_tail(self, job: CrawlJobState, mark: int) -> list[dict[str, object]]:
        """The queue tail beyond the persisted mark, as dicts for digesting."""
        return [{"url": entry.url.raw, "depth": entry.depth} for entry in islice(job.frontier, mark, None)]

    def _frontier_resync(self, job_id: str) -> None:
        """Drop the whole frontier table; caller re-inserts from mark 0."""
        self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))

    def _frontier_write_tail(self, job_id: str, mark: int, tail: list[dict[str, object]]) -> None:
        """Append the frontier tail starting after the table's current high-water pos."""
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
        """Reconcile the live queue against its table rows.

        ``frontier_removed`` names rows to delete; ``frontier_mark`` splits the
        persisted prefix from the appended tail: a batch is O(removed+appended).
        """
        job_id = job.job_id
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
            # Tail empty: no-op and same-length replacement share mark + tail;
            # the full-digest tie-break separates them when one is on file.
            full_digest = self._digest(
                [{"url": entry.url.raw, "depth": entry.depth} for entry in job.frontier]
            )
            if self._content_full_matches(job_id, "frontier", full_digest):
                # Content identical to the persisted table: nothing to write.
                job.frontier_mark = len(job.frontier)
                self._record_content(job_id, "frontier", len(job.frontier), self._digest([]), full_digest)
                return
            stored = self._stored(job_id, "frontier")
            if stored is not None and stored[2] is None:
                # Post-growth no-op: table already equals live content.
                skip_write = True
            else:
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

    def _append_results(self, job: CrawlJobState) -> None:
        """Insert only the pages this batch added, keyed off the table's high-water
        mark (not a remembered count). Shrunken history or a same-length replacement
        is detected by content identity and resynced."""
        job_id = job.job_id
        results = job.results
        table = self._table_rows.get("results", []) if self._table_rows_job == job_id else []
        persisted = self._max_seq_plus_one(job_id)
        tail = results[persisted:]
        tail_digest = self._digest(tail)

        if persisted < len(results):
            # Growth: digest only the new tail (O(new rows)).
            if self._content_matches(job_id, "results", persisted, tail_digest):
                # No-op: the tail is already persisted. Keep the stored full digest.
                return
        else:
            # Ambiguous: tail empty -- a no-op and a same-length replacement look
            # identical to mark + tail; whole-collection digest tie-breaks.
            full_digest = self._digest(results)
            if self._content_full_matches(job_id, "results", full_digest):
                # No-op: the table already holds the live history.
                self._record_content(job_id, "results", len(results), self._digest([]), full_digest)
                return
            stored = self._stored(job_id, "results")
            if stored is not None and stored[2] is None:
                # Post-growth no-op: nothing rewritten since the last write.
                return
            # Same-length replacement: fall through to resync below.
        if table and self._digest(results[: len(table)]) != self._digest(table):
            self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            table = []
        row = self._conn.execute(
            'SELECT max(seq) FROM "crawl_job_results" WHERE job_id = ?', (job_id,)
        ).fetchone()
        persisted = 0 if row["max(seq)"] is None else row["max(seq)"] + 1
        if persisted >= len(results):
            # Shrank or same-length replacement: resync so the table matches exactly.
            self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            persisted = 0
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
            ((job_id, seq, _json(result)) for seq, result in enumerate(results[persisted:], start=persisted)),
        )
        self._record_content(job_id, "results", len(results), self._digest([]), None)

    def _append_visited(self, job: CrawlJobState) -> None:
        """Insert only the visited tail this state has not sent yet, keyed off
        the live ``visited_mark`` (the ``_append_results`` pattern): the slice
        after the mark is exactly what is new; replacements resync by identity."""
        job_id = job.job_id
        mark = job.visited_mark
        tail_urls = [url.raw for url in islice(job.visited, mark, None)]
        tail_digest = self._digest(tail_urls)

        skip_write = False
        if mark > len(job.visited):
            # The set shrank below the mark (retry-requeue discard) resyncs,
            # and INSERT OR IGNORE stays: the ignore is the cross-process guard.
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
                return
            stored = self._stored(job_id, "visited")
            if stored is not None and stored[2] is None:
                # Post-growth no-op: table already equals live content.
                skip_write = True
            else:
                # Same-length replacement: resync.
                self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
                mark = 0
        elif self._content_matches(job_id, "visited", mark, tail_digest):
            # Growth tail already persisted: nothing to write.
            skip_write = True
        if not skip_write:
            self._conn.executemany(
                'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
                ((job_id, url.raw) for url in islice(job.visited, mark, None)),
            )
        job.visited_mark = len(job.visited)
        self._record_content(job_id, "visited", len(job.visited), self._digest([]), None)


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=json_default)
