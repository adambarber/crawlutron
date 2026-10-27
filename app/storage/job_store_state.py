"""The crawl-job record, the row's write spelling, and the accumulators.

``_job_row_params`` is the only place the record flattens to the store's JSON
spelling; the read side (``job_store_read``) consumes that declared shape.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from itertools import islice
from typing import TYPE_CHECKING, Any, TypedDict

from app.admission.policy import PolicyTier, read_policy
from app.admission.policy import stored_policy as _stored_policy
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.safety.json_safe import json_default

from .job_store_collections import (
    FrontierEntry,
    FrontierEntryDict,
    JobKind,
    JobStatus,
    OrderedUrlSet,
    _entry_payload,
    normalize_frontier,
    normalize_visited,
    read_transient_retries,
)

if TYPE_CHECKING:
    from .job_store import JobStore

_STATS_KEYS = ("total_crawled", "total_failed", "cap_truncated", "cap_overcommitted")


def _default_stats() -> dict[str, int]:
    return {key: 0 for key in _STATS_KEYS}


def _normalized_stats(value: Any) -> dict[str, int]:
    stats = _default_stats()
    if isinstance(value, dict):
        for key in (*_STATS_KEYS, "total_discovered"):
            # ``total_discovered`` is reported only when the whole-site runner
            # counted admissions; a stored value is preserved, never seeded zero.
            if key == "total_discovered" and key not in value:
                continue
            raw = value.get(key, 0)
            if isinstance(raw, bool):
                continue
            try:
                stats[key] = int(raw)
            except (TypeError, ValueError):
                continue
    return stats


class CrawlJobRow(TypedDict):
    """The row one load reads, declared field by field.

    The inverse of ``_job_row_params``' write spelling: every ``json.loads``
    column lands here under its declared type, not as an undeclared guess.
    """

    job_id: str
    kind: str
    owner: str
    root_url: str
    policy: dict[str, JsonValue]
    status: str
    frontier: list[FrontierEntryDict]
    visited: list[str]
    results: list[dict[str, JsonValue]]
    created_at: float
    updated_at: float
    error: str | None
    stats: dict[str, int]
    in_flight: list[FrontierEntryDict]
    transient_retries: dict[str, int]


@dataclass
class CrawlJobState:
    """Persistent state of a crawl job (whole-site or single-shot)."""

    job_id: str
    kind: JobKind = JobKind.WHOLE_SITE
    root_url: str = ""
    owner: str = ""
    # The typed model, kept whole through the live job; only the write edge
    # (``_job_row_params``) flattens it to the store's JSON spelling.
    policy: CrawlPolicy = field(default_factory=CrawlPolicy)
    status: JobStatus = JobStatus.PENDING
    # Whole-site fields (used when kind == WHOLE_SITE)
    frontier: list[FrontierEntry] = field(default_factory=list)
    # The live-only frontier sync (#547): ``frontier_mark`` is the persisted prefix and
    # ``frontier_removed`` names the rows the next persist deletes (loads re-derive both).
    frontier_mark: int = 0
    frontier_removed: list[NormalizedUrl] = field(default_factory=list)
    # Insertion-ordered set: the persist delta slices by position; only a
    # stable order makes that sound (see OrderedUrlSet).
    visited: OrderedUrlSet = field(default_factory=OrderedUrlSet)
    # High-water of what this process already sent: 0 on create, len on load
    # -- never the table's count, which a sibling's inserts make lie (#140).
    visited_mark: int = 0
    in_flight: list[FrontierEntry] = field(default_factory=list)
    transient_retries: dict[NormalizedUrl, int] = field(default_factory=dict)  # admitted url -> retry count
    # Single-shot fields (kind == SINGLE_SHOT): root_url holds the first URL (or
    # "" for batch); results holds the response model's own dump per entry.
    results: list[dict[str, JsonValue]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    stats: dict[str, int] = field(default_factory=_default_stats)
    host_policy: HostPolicy = field(default_factory=HostPolicy.unrestricted, repr=False, compare=False)
    # Deployment tier held on the row: a job outlives its request, and a stored
    # policy that omitted a limit must resolve as the operator's, not the default.
    tier: PolicyTier = field(default_factory=PolicyTier.model_defaults, repr=False, compare=False)

    @classmethod
    def from_json(cls, data: CrawlJobRow, *, host_policy: HostPolicy, tier: PolicyTier) -> CrawlJobState:
        """Build one live record from the declared row record.

        Collections normalize here; the cross-list identity re-derivation runs
        once per load at the read walk.
        """
        status = data.get("status", JobStatus.PENDING.value)
        kind = data.get("kind", JobKind.WHOLE_SITE.value)
        policy = read_policy(data["policy"], tier)
        root_url = str(data.get("root_url") or "")
        admitted_root = admit_or_none(root_url, policy=host_policy)
        root_url = admitted_root.raw if admitted_root is not None else ""
        # The queue's rows are url-keyed, so a duplicate identity in the stored
        # entries leaves a row the merged list no longer holds (#547).
        frontier, deduped = normalize_frontier(
            data.get("frontier", []),
            root_url=root_url,
            policy=policy,
            host_policy=host_policy,
        )
        job = cls(
            job_id=str(data.get("job_id") or ""),
            kind=JobKind(kind),
            root_url=root_url,
            owner=str(data.get("owner") or ""),
            policy=policy,
            status=JobStatus(status),
            frontier=frontier,
            visited=normalize_visited(list(data.get("visited", [])), host_policy=host_policy),
            in_flight=normalize_frontier(
                data.get("in_flight", []),
                root_url=root_url,
                policy=policy,
                host_policy=host_policy,
            )[0],
            transient_retries=read_transient_retries(
                dict(data.get("transient_retries", {})), host_policy=host_policy
            ),
            results=list(data.get("results", [])),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            error=data.get("error") if isinstance(data.get("error"), str) else None,
            stats=_normalized_stats(data.get("stats", {})),
            host_policy=host_policy,
            tier=tier,
        )
        # A load's visited set IS the persisted set: this process starts its
        # delta at the end of it (a create starts at 0 -- the default).
        job.visited_mark = len(job.visited)
        # The queue is the same story (#547): every loaded entry has its row,
        # except the identities the normalize deduped away (#547).
        job.frontier_mark = len(job.frontier)
        job.frontier_removed.extend(NormalizedUrl(raw, policy=host_policy) for raw in deduped)
        return job


_UPSERT_JOB_ROW_SQL = """
      INSERT INTO crawl_jobs (job_id, kind, root_url, policy, status,
                               created_at, updated_at, error, stats, in_flight, transient_retries,
                               owner)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id) DO UPDATE SET
    kind=excluded.kind,
    root_url=excluded.root_url,
    policy=excluded.policy,
    updated_at=excluded.updated_at,
    error=excluded.error,
          stats=excluded.stats,
          in_flight=excluded.in_flight,
          transient_retries=excluded.transient_retries
"""


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=json_default)


def _retries_payload(retries: dict[NormalizedUrl, int]) -> dict[str, int]:
    """Wire keys for the retry counter: the persisted column is text-keyed."""
    return {url.raw: attempts for url, attempts in retries.items()}


def _job_row_params(job: CrawlJobState) -> tuple:
    """The live-state columns of the job row, in ``_UPSERT_JOB_ROW_SQL`` order.
    Separate from the statement so the two cannot drift: a column added to the
    SQL without a matching entry here is a TypeError at first persist, not a silent drop.
    """
    return (
        job.job_id,
        job.kind.value,
        job.root_url,
        _json(_stored_policy(job.policy)),
        job.status.value,
        job.created_at,
        job.updated_at,
        job.error,
        _json(job.stats),
        _json([_entry_payload(entry) for entry in job.in_flight]),
        _json(_retries_payload(job.transient_retries)),
        job.owner,
    )


class JobAccumulators:
    """The growing part of a job: its append-only page history, visited set and
    frontier queue, in their own tables so a batch is one INSERT per new row
    (row is batch-bounded: frontier pops per batch, in_flight by concurrency)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def read_results(self, job_id: str) -> list[dict[str, JsonValue]]:
        """The page history, in the order it was crawled.

        Each payload is the response model's own dump (the write edge), so the
        declared read is the dump's shape, not ``Any``.
        """
        rows = self._conn.execute(
            'SELECT payload FROM "crawl_job_results" WHERE job_id = ? ORDER BY seq', (job_id,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def read_visited(self, job_id: str) -> list[str]:
        rows = self._conn.execute(
            'SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
        ).fetchall()
        return [row["url"] for row in rows]

    def read_frontier(self, job_id: str) -> list[FrontierEntryDict]:
        """The outstanding queue, in queue order.

        Rows are keyed by URL, so a read is every entry the job still holds;
        the identity merge on read filters what admission no longer keeps.
        """
        rows = self._conn.execute(
            'SELECT url, depth FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos', (job_id,)
        ).fetchall()
        return [{"url": row["url"], "depth": row["depth"]} for row in rows]

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
        """Reconcile the live queue against its table rows (#547).

        ``frontier_removed`` names rows to delete, ``frontier_mark`` splits the
        persisted prefix from the appended tail: a batch is O(removed+appended).
        """
        job_id = job.job_id
        for url in job.frontier_removed:
            self._conn.execute(
                'DELETE FROM "crawl_job_frontier" WHERE job_id = ? AND url = ?', (job_id, url.raw)
            )
        job.frontier_removed.clear()
        mark = job.frontier_mark
        if mark > len(job.frontier):
            # The live list shrank below the persisted prefix by a mutation that
            # logged no removal -- a wholesale replacement. Resync, O(queue).
            self._conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,))
            mark = 0
        if mark == len(job.frontier):
            job.frontier_mark = mark
            return
        row = self._conn.execute(
            'SELECT max(pos) AS high FROM "crawl_job_frontier" WHERE job_id = ?', (job_id,)
        ).fetchone()
        high = row["high"]
        start = 0 if high is None else high + 1
        self._conn.executemany(
            # OR REPLACE, not OR IGNORE: a re-inserted identity carries its new
            # depth and position (#547) -- the write edge's decision, not the table's.
            'INSERT OR REPLACE INTO "crawl_job_frontier" (job_id, url, pos, depth) VALUES (?, ?, ?, ?)',
            (
                (job_id, entry.url.raw, pos, entry.depth)
                for pos, entry in enumerate(islice(job.frontier, mark, None), start=start)
            ),
        )
        job.frontier_mark = len(job.frontier)

    def _append_results(self, job: CrawlJobState) -> None:
        """Insert only the pages this batch added, keyed off the table's high-water mark
        (not a remembered count, so a resumed process still appends exactly what it
        hasn't written). If normalization shrank history, resync rather than silently skip.
        """
        job_id = job.job_id
        results = job.results
        row = self._conn.execute(
            'SELECT max(seq) FROM "crawl_job_results" WHERE job_id = ?', (job_id,)
        ).fetchone()
        persisted = 0 if row["max(seq)"] is None else row["max(seq)"] + 1
        if persisted > len(results):
            self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
            persisted = 0
        if persisted == len(results):
            return
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
            ((job_id, seq, _json(result)) for seq, result in enumerate(results[persisted:], start=persisted)),
        )

    def _append_visited(self, job: CrawlJobState) -> None:
        """Insert only the visited tail this state has not sent yet, keyed off
        the live ``visited_mark`` (the ``_append_results`` pattern): the set is
        insertion-ordered, so the slice after the mark is exactly what is new.
        """
        job_id = job.job_id
        # A set that shrank below the mark (retry-requeue discard) resyncs, and
        # INSERT OR IGNORE stays: the ignore is the #140 cross-process guard.
        if job.visited_mark > len(job.visited):
            self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
            job.visited_mark = 0
        mark = job.visited_mark
        if mark == len(job.visited):
            return
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
            ((job_id, url.raw) for url in islice(job.visited, mark, None)),
        )
        job.visited_mark = len(job.visited)


def _create_job(
    store: JobStore,
    job_id: str,
    kind: JobKind,
    root_url: str,
    policy: CrawlPolicy,
    frontier: list[FrontierEntry] | None = None,
    *,
    owner: str = "",
) -> CrawlJobState:
    """The one place a job is born, for both kinds, and the one place it is written.
    A module function so the row's content and the locked write are one decision;
    the two kinds used to split across that boundary and drift (one lost its tier).
    """
    job = CrawlJobState(
        job_id=job_id,
        kind=kind,
        root_url=root_url,
        owner=owner,
        policy=policy,
        status=JobStatus.PENDING,
        frontier=frontier or [],
        host_policy=store._host_policy,
        tier=store.tier,
    )
    with store._lock:
        store._persist(job)
    return job


__all__ = [
    "CrawlJobState",
    "JobAccumulators",
]


_CAS_STATUS_SQL = """
      -- json_each keeps the expected set a BOUND parameter; COALESCE keeps the
      -- stored error on an error-less CAS (the stamp rides the CAS, #365).
      UPDATE crawl_jobs SET status = ?, updated_at = ?,
                             error = COALESCE(?, error)
      WHERE job_id = ? AND deleted_at IS NULL
        AND status IN (SELECT value FROM json_each(?))
"""
