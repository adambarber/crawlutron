from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from app.admission.policy import PolicyTier, read_policy
from app.admission.policy import stored_policy as _stored_policy
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.safety.json_safe import json_default

from .job_store_collections import (
    FrontierEntry,
    JobKind,
    JobStatus,
    _entry_payload,
    normalize_frontier,
    normalize_job_collections,
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


@dataclass
class CrawlJobState:
    """Persistent state of a crawl job (whole-site or single-shot)."""

    job_id: str
    kind: JobKind = JobKind.WHOLE_SITE
    root_url: str = ""
    owner: str = ""
    policy: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    # Whole-site fields (used when kind == WHOLE_SITE)
    frontier: list[FrontierEntry] = field(default_factory=list)
    visited: set[NormalizedUrl] = field(default_factory=set)
    in_flight: list[FrontierEntry] = field(default_factory=list)
    transient_retries: dict[NormalizedUrl, int] = field(default_factory=dict)  # admitted url -> retry count
    # Single-shot fields (used when kind == SINGLE_SHOT)
    # root_url holds the first URL (or "" for batch); results holds completed crawl results
    results: list[dict[str, Any]] = field(default_factory=list)  # Completed results
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    stats: dict[str, int] = field(default_factory=_default_stats)
    host_policy: HostPolicy = field(default_factory=HostPolicy.unrestricted, repr=False, compare=False)
    # Deployment tier held on the row: a job outlives its request, and a stored
    # policy that omitted a limit must resolve as the operator's, not the default.
    tier: PolicyTier = field(default_factory=PolicyTier.model_defaults, repr=False, compare=False)

    @classmethod
    def from_json(cls, data: dict[str, Any], *, host_policy: HostPolicy, tier: PolicyTier) -> CrawlJobState:
        """Deserialize from JSON dict; the read boundary always normalizes canonical
        identity, cross-list dedupe and depths, so no writer can reintroduce a second
        identity for a page. One path, no persisted ``canonicalized`` flag (migration 0002 removed it).
        """
        status = data.get("status", JobStatus.PENDING.value)
        kind = data.get("kind", JobKind.WHOLE_SITE.value)
        policy = read_policy(data.get("policy"), tier)
        root_url = str(data.get("root_url") or "")
        admitted_root = admit_or_none(root_url, policy=host_policy)
        root_url = admitted_root.raw if admitted_root is not None else ""
        visited_raw = data.get("visited", [])
        if isinstance(visited_raw, list):
            visited = {
                admitted
                for url in visited_raw
                if isinstance(url, str) and url
                for admitted in (admit_or_none(url, policy=host_policy),)
                if admitted is not None
            }
        else:
            visited = set()
        transient_raw = data.get("transient_retries", {})
        transient_retries: dict[NormalizedUrl, int] = {}
        if isinstance(transient_raw, dict):
            for url, attempts in transient_raw.items():
                if not isinstance(url, str):
                    continue
                try:
                    key = admit_or_none(url, policy=host_policy)
                    if key is not None:
                        transient_retries[key] = max(0, int(attempts))
                except (TypeError, ValueError):
                    continue
        job = cls(
            job_id=str(data.get("job_id") or ""),
            kind=JobKind(kind),
            root_url=root_url,
            owner=str(data.get("owner") or ""),
            policy=_stored_policy(policy),
            status=JobStatus(status),
            frontier=normalize_frontier(
                data.get("frontier", []),
                root_url=root_url,
                policy=policy,
                host_policy=host_policy,
            ),
            visited=visited,
            in_flight=normalize_frontier(
                data.get("in_flight", []),
                root_url=root_url,
                policy=policy,
                host_policy=host_policy,
            ),
            transient_retries=transient_retries,
            results=list(data.get("results", [])) if isinstance(data.get("results", []), list) else [],
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            error=data.get("error") if isinstance(data.get("error"), str) else None,
            stats=_normalized_stats(data.get("stats", {})),
            host_policy=host_policy,
            tier=tier,
        )
        # from_json establishes identity; this re-derives it over the assembled
        # collections once per load, so no writer can leave a row reading as two.
        normalize_job_state(job)
        return job


def normalize_job_state(job: CrawlJobState) -> CrawlJobState:
    admitted_root = admit_or_none(job.root_url, policy=job.host_policy)
    root_url = admitted_root.raw if admitted_root is not None else ""
    policy = read_policy(job.policy, job.tier)
    visited, frontier, in_flight = normalize_job_collections(
        root_url=root_url,
        policy=policy,
        visited=job.visited,
        frontier=job.frontier,
        in_flight=job.in_flight,
        host_policy=job.host_policy,
    )
    transient_retries: dict[NormalizedUrl, int] = {}
    for url, attempts in job.transient_retries.items():
        try:
            value = max(0, int(attempts))
        except (TypeError, ValueError):
            continue
        transient_retries[url] = max(transient_retries.get(url, 0), value)
    job.root_url = root_url
    job.policy = _stored_policy(policy)
    job.visited = visited
    job.frontier = frontier
    job.in_flight = in_flight
    job.transient_retries = transient_retries
    job.stats = _normalized_stats(job.stats)
    return job


_UPSERT_JOB_ROW_SQL = """
      INSERT INTO crawl_jobs (job_id, kind, root_url, policy, status, frontier,
                               created_at, updated_at, error, stats, in_flight, transient_retries,
                               owner)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id) DO UPDATE SET
    kind=excluded.kind,
    root_url=excluded.root_url,
    policy=excluded.policy,
    frontier=excluded.frontier,
    updated_at=excluded.updated_at,
    error=excluded.error,
          stats=excluded.stats,
          in_flight=excluded.in_flight,
          transient_retries=excluded.transient_retries
"""

# Every field the job response exposes, shared by single and list reads so the
# two cannot drift into describing different jobs.
_JOB_STATUS_PROJECTION = """
    SELECT job_id, root_url, status, created_at, updated_at, stats, error, owner,
           COALESCE(kind, 'whole_site') AS kind,
           json_array_length(frontier) AS frontier_size,
           COALESCE(json_extract(stats, '$.cap_truncated'), 0) AS cap_truncated
      FROM crawl_jobs
"""


class JobStatusView(TypedDict):
    """The status view: one record, one declared shape, every key mandatory.
    A TypedDict so the compiler catches a view/response-model key mismatch (the
    old dict[str, Any] dropped kind silently, which recovery read as whole-site).
    """

    job_id: str
    root_url: str
    owner: str
    status: str
    kind: str
    created_at: float
    updated_at: float
    frontier_size: int
    visited_size: int
    results_count: int
    cap_truncated: bool
    stats: dict[str, int]
    error: str | None


def _status_row(row: sqlite3.Row, visited: int, results: int) -> JobStatusView:
    """Shape one projected row into the status view the API returns: frontier_size
    and cap_truncated are NULL on a fresh job, and ``kind`` is selected because
    recovery reads it from this view (it was missing, so every job read as whole-site).
    """
    return {
        "job_id": row["job_id"],
        "root_url": row["root_url"],
        "owner": row["owner"],
        "status": row["status"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "frontier_size": row["frontier_size"] or 0,
        "visited_size": visited,
        "results_count": results,
        "cap_truncated": bool(row["cap_truncated"]),
        "stats": json.loads(row["stats"]) if row["stats"] else {},
        "error": row["error"],
    }


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
        _json(job.policy),
        job.status.value,
        _json([_entry_payload(entry) for entry in job.frontier]),
        job.created_at,
        job.updated_at,
        job.error,
        _json(job.stats),
        _json([_entry_payload(entry) for entry in job.in_flight]),
        _json(_retries_payload(job.transient_retries)),
        job.owner,
    )


class JobAccumulators:
    """The growing part of a job: its append-only page history and visited set.
    They live in their own tables so a batch is one INSERT per new page, not a
    re-serialization of everything crawled (the live row is rewritten wholesale).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def read_results(self, job_id: str) -> list[Any]:
        """The page history, in the order it was crawled.

        ``seq`` is the crawl order and the insert key at once, so reading the
        history back is one ordered index scan rather than a sort.
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

    def append(self, job: CrawlJobState) -> None:
        self._append_results(job)
        self._append_visited(job)

    def delete(self, job_id: str) -> None:
        """Drop a job's history.
        Without this, the tables would keep a deleted job's crawl with nothing
        pointing at it -- the old column layout deleted the history by construction.
        """
        self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
        self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))

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
        """Insert newly visited pages. The count check skips no-growth batches; on
        growth the INSERT OR IGNORE collapses a page visited twice to one row (set
        semantics = the primary key). "New" is a set difference, not a slice.
        """
        job_id = job.job_id
        visited = job.visited
        stored = self._conn.execute(
            'SELECT count(*) AS n FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
        ).fetchone()["n"]
        if len(visited) <= stored:
            return
        already = {
            row["url"]
            for row in self._conn.execute('SELECT url FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))
        }
        fresh = visited - already
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
            ((job_id, url.raw) for url in fresh),
        )


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
        policy=_stored_policy(policy),
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
    "JobStatusView",
    "normalize_job_state",
]
