"""#3+#14 Resumable whole-site crawl job persistence (SQLite-backed).

Provides a job store for managing long-running crawl jobs with pause/resume
capability. Jobs persist their frontier, visited set, and results to SQLite
so they can survive process restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.api_models import CrawlPolicy
from app.crawl.ids import new_job_id
from app.crawl.migrations import StoreSchema
from app.crawl.policy import PolicyTier, read_policy
from app.crawl.policy import stored_policy as _stored_policy
from app.crawl.sqlite_store import connect_store
from app.validation import HostPolicy, normalize_public_http_url, safe_public_url_or_none

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JobStore save fallback -- now the shared app.json_safe chokepoint."""
    from app.json_safe import json_default

    return json_default(obj)


class JobKind(StrEnum):
    """Kind of crawl job."""

    WHOLE_SITE = "whole_site"  # Recursive crawl with frontier/visited
    SINGLE_SHOT = "single_shot"  # Single async crawl (no frontier/visited)


class JobStatus(StrEnum):
    """Lifecycle status of a crawl job."""

    PENDING = "pending"  # Created, not yet started
    RUNNING = "running"  # Actively crawling
    PAUSED = "paused"  # Paused by user
    COMPLETED = "completed"  # Frontier exhausted / crawl finished
    FAILED = "failed"  # Fatal error
    CANCELLED = "cancelled"  # Explicitly cancelled


#: The states a compare-and-set accepts: one status, or the set a transition is
#: legal from. Named once so the sync method and its async twin cannot diverge.
_Expected = JobStatus | frozenset[JobStatus]


@dataclass(frozen=True)
class FrontierEntry:
    """One canonical URL and its admitted crawl depth."""

    url: str
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "depth": self.depth}


# Cap on memoized canonical-identity entries. The job store re-reads the same
# durable URL set on every loop iteration; the cap keeps a very large crawl's
# working set bounded while covering the repeated reads that made the old
# per-read re-derivation expensive.
_CANONICAL_CACHE_MAX = 50_000


@lru_cache(maxsize=_CANONICAL_CACHE_MAX)
def _canonical_cached(url: str, host_policy: HostPolicy) -> str:
    return str(safe_public_url_or_none(url, policy=host_policy) or url)


def _canonical_or_raw(url: str, host_policy: HostPolicy) -> str:
    """Canonical page identity for ``url``, or the raw value when it has none.

    Memoized: the job store re-reads the same durable collections on every loop
    iteration, and re-deriving identity for every stored URL on every read made
    a 20k-entry job cost ~0.23s per load and stalled the event loop with it. The
    mapping is a pure function of the URL and the process's SSRF posture (which
    is itself resolved once per process), so a bounded cache cannot disagree
    with the uncached result.
    """
    if not url:
        return url
    return _canonical_cached(url, host_policy)


# A persisted row is ``json.loads`` of a TEXT column, so its collections are
# whatever some earlier writer persisted: possibly absent, possibly a bare
# string, possibly a list of dicts, possibly corrupt. ``object`` -- not ``Any``
# -- is the honest annotation, and it is load-bearing rather than cosmetic: with
# ``Any`` the isinstance narrowing below is invisible to a type checker, so a
# change to the persisted shape would type-check and then misbehave at runtime.
_PersistedJSON = object


# The read edge is :func:`app.crawl.policy.read_policy` and it has exactly one
# name. This module used to export ``normalized_policy`` as its own copy of that
# function, which made the shared read edge a two-home surface: importers picked
# up whichever spelling their file already imported, and a future change to the
# repair rules had two definitions to keep in step. The three limit readers
# below stay here because job-path callers read a single bound at a time and
# spelling the hop keeps that legible.


def job_max_pages(policy: Any, tier: PolicyTier | None = None) -> int:
    return read_policy(policy, tier).limits.max_pages


def _entry_depth(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _entry_payload(entry: Any) -> Any:
    """Serialize a frontier entry, tolerating the legacy bare-string shape.

    The store does not rewrite a caller's collections: a row written with bare
    strings stays bare, and :meth:`CrawlJobState.from_json` recognizes that
    shape as legacy and normalizes it once on read. Coercing here instead would
    make a non-canonical row LOOK canonical to that structural check, which is
    how a stored "already canonical" flag becomes a lie.
    """
    return entry.to_dict() if isinstance(entry, FrontierEntry) else entry


def normalize_frontier(
    values: _PersistedJSON,
    *,
    root_url: str,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> list[FrontierEntry]:
    """Coerce persisted/runner-supplied frontier values into ``FrontierEntry`` records.

    Canonicalization is unconditional on the read path: it still coerces shape,
    keeps the shallowest depth per identity, and drops unusable entries. It
    used to be skipped on the strength of a persisted ``canonicalized`` column,
    but a stored flag can disagree with the row it claims to describe (a direct
    SQL write, a migration, an older writer) and then the read path skips
    normalization on a row that needs it; migration 0002 dropped the column,
    which is why the flag could not be trusted. The cost is bounded by the
    canonicalization memo in :func:`_canonical_or_raw`.
    """
    if not isinstance(values, (list, tuple)):
        return []
    canonical_root = _canonical_or_raw(root_url, host_policy)
    legacy_depth = policy.limits.max_depth
    entries: dict[str, FrontierEntry] = {}
    order: list[str] = []
    for value in values:
        raw_url: object
        depth: object
        if isinstance(value, FrontierEntry):
            raw_url = value.url
            depth = value.depth
        elif isinstance(value, dict):
            raw_url = value.get("url")
            depth = value.get("depth", legacy_depth)
        else:
            raw_url = value
            depth = legacy_depth
        if not isinstance(raw_url, str) or not raw_url:
            continue
        url = _canonical_or_raw(raw_url, host_policy)
        entry_depth = 0 if url == canonical_root else _entry_depth(depth, legacy_depth)
        entry = FrontierEntry(url=url, depth=entry_depth)
        previous = entries.get(url)
        if previous is None:
            entries[url] = entry
            order.append(url)
        elif entry_depth < previous.depth:
            entries[url] = entry
    return [entries[url] for url in order]


def normalize_job_collections(
    *,
    root_url: str,
    policy: CrawlPolicy,
    visited: _PersistedJSON,
    frontier: _PersistedJSON,
    in_flight: _PersistedJSON,
    host_policy: HostPolicy,
) -> tuple[set[str], list[FrontierEntry], list[FrontierEntry]]:
    canonical_root = _canonical_or_raw(root_url, host_policy)
    if isinstance(visited, str):
        visited_values = [visited]
    elif isinstance(visited, Iterable):
        visited_values = list(visited)
    else:
        # A persisted column that is not a sequence of URLs is not a crash
        # worth raising: the job loses its visited history, and the frontier
        # below still bounds what it will fetch.
        visited_values = []
    normalized_visited = {
        _canonical_or_raw(url, host_policy) for url in visited_values if isinstance(url, str) and url
    }
    normalized_frontier = normalize_frontier(
        frontier,
        root_url=canonical_root,
        policy=policy,
        host_policy=host_policy,
    )
    normalized_in_flight = normalize_frontier(
        in_flight,
        root_url=canonical_root,
        policy=policy,
        host_policy=host_policy,
    )
    frontier_by_url = {entry.url: entry for entry in normalized_frontier}
    normalized_in_flight = [
        FrontierEntry(
            url=entry.url,
            depth=min(entry.depth, frontier_by_url[entry.url].depth)
            if entry.url in frontier_by_url
            else entry.depth,
        )
        for entry in normalized_in_flight
        if entry.url not in normalized_visited
    ]
    in_flight_ids = {entry.url for entry in normalized_in_flight}
    normalized_frontier = [
        entry
        for entry in normalized_frontier
        if entry.url not in normalized_visited and entry.url not in in_flight_ids
    ]
    return normalized_visited, normalized_frontier, normalized_in_flight


# Stats a job row always carries. ``total_discovered`` is deliberately NOT in
# this tuple: it is frontier-derived, so only the whole-site runner may report
# it, and a single-shot job has no frontier to derive it from.
_STATS_KEYS = ("total_crawled", "total_failed", "cap_truncated", "cap_overcommitted")


def _default_stats() -> dict[str, int]:
    return {key: 0 for key in _STATS_KEYS}


def _normalized_stats(value: Any) -> dict[str, int]:
    stats = _default_stats()
    if isinstance(value, dict):
        for key in (*_STATS_KEYS, "total_discovered"):
            # ``total_discovered`` is frontier-derived and is reported only when
            # the whole-site runner actually counted admissions. Seeding it to 0
            # for every job kind makes a single-shot job -- which has no
            # frontier, so the quantity is undefined rather than zero -- report
            # a fabricated zero; that contract belongs to
            # app/api/async_crawl_runner.py. A value already stored (whole-site
            # rows) is preserved.
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
    policy: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    # Whole-site fields (used when kind == WHOLE_SITE)
    frontier: list[FrontierEntry] = field(default_factory=list)
    visited: set[str] = field(default_factory=set)
    in_flight: list[FrontierEntry] = field(default_factory=list)
    transient_retries: dict[str, int] = field(default_factory=dict)  # url -> transient retry count
    # Single-shot fields (used when kind == SINGLE_SHOT)
    # root_url holds the first URL (or "" for batch); results holds completed crawl results
    results: list[dict[str, Any]] = field(default_factory=list)  # Completed results
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    stats: dict[str, int] = field(default_factory=_default_stats)
    host_policy: HostPolicy = field(default_factory=HostPolicy.unrestricted, repr=False, compare=False)
    # Which deployment wrote this row, so re-resolving its policy means the same
    # thing here as it did on the read that loaded it. Held on the row rather
    # than passed to each read because a job row outlives the request that
    # created it: threading the tier through every read site is how every one of
    # them ended up passing nothing, and a stored policy that omitted a limit
    # then resolved to the model default instead of the operator's.
    tier: PolicyTier = field(default_factory=PolicyTier.model_defaults, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict.

        Normalizes first, so the serialized form carries canonical page identity
        (the read boundary enforces it too, but a writer that emits an alias
        makes every reader pay for it). This is affordable only because
        canonicalization is memoized in :func:`_canonical_or_raw`: the same
        durable URL set is re-serialized on every job loop iteration.
        """
        normalize_job_state(self)
        data = asdict(self)
        data.pop("host_policy", None)
        data.pop("tier", None)
        data["frontier"] = [_entry_payload(entry) for entry in self.frontier]
        data["in_flight"] = [_entry_payload(entry) for entry in self.in_flight]
        data["visited"] = sorted(self.visited)
        data["stats"] = _normalized_stats(self.stats)
        data["status"] = self.status.value
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any], *, host_policy: HostPolicy, tier: PolicyTier) -> CrawlJobState:
        """Deserialize from JSON dict.

        The read boundary always normalizes: canonical identity, cross-list
        dedupe, shallowest depth and legacy bare-string depths are enforced
        here, so a row written by any writer -- including one that put
        non-canonical URLs inside well-formed records -- cannot reintroduce a
        second identity for a page. The cost is bounded by the canonicalization
        memo in :func:`_canonical_or_raw` rather than by skipping the work.

        This used to branch on a persisted ``canonicalized`` column, read from
        a local that was hardcoded ``False`` -- the column is gone (migration
        0002), so the branch could only ever take the normalizing path, and
        keeping it invited a second writer to "optimize" a flag that no longer
        exists. One path, no flag.
        """
        status = data.get("status", JobStatus.PENDING.value)
        kind = data.get("kind", JobKind.WHOLE_SITE.value)
        policy = read_policy(data.get("policy"), tier)
        root_url = str(data.get("root_url") or "")
        root_url = _canonical_or_raw(root_url, host_policy)
        visited_raw = data.get("visited", [])
        if isinstance(visited_raw, list):
            visited = {
                _canonical_or_raw(url, host_policy) for url in visited_raw if isinstance(url, str) and url
            }
        else:
            visited = set()
        transient_raw = data.get("transient_retries", {})
        transient_retries: dict[str, int] = {}
        if isinstance(transient_raw, dict):
            for url, attempts in transient_raw.items():
                if not isinstance(url, str):
                    continue
                try:
                    key = _canonical_or_raw(url, host_policy)
                    transient_retries[key] = max(0, int(attempts))
                except (TypeError, ValueError):
                    continue
        job = cls(
            job_id=str(data.get("job_id") or ""),
            kind=JobKind(kind),
            root_url=root_url,
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
        # Full normalization pass on every read. from_json establishes the
        # identity, and this re-derives it over the assembled collections
        # (cross-list dedupe, shallowest depth) exactly once per load, so no
        # writer can leave a row that reads as two identities for one page.
        normalize_job_state(job)
        return job


def normalize_job_state(job: CrawlJobState) -> CrawlJobState:
    root_url = _canonical_or_raw(job.root_url, job.host_policy)
    policy = read_policy(job.policy, job.tier)
    visited, frontier, in_flight = normalize_job_collections(
        root_url=root_url,
        policy=policy,
        visited=job.visited,
        frontier=job.frontier,
        in_flight=job.in_flight,
        host_policy=job.host_policy,
    )
    transient_retries: dict[str, int] = {}
    for url, attempts in job.transient_retries.items():
        if not isinstance(url, str) or not url:
            continue
        try:
            value = max(0, int(attempts))
        except (TypeError, ValueError):
            continue
        identity = _canonical_or_raw(url, job.host_policy)
        transient_retries[identity] = max(transient_retries.get(identity, 0), value)
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
                              created_at, updated_at, error, stats, in_flight, transient_retries)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


# The status view: every field the job response exposes, and nothing that would
# make reading it cost what reading the job costs. Shared by the single and the
# list read so the two cannot drift -- a field added to one and not the other
# would let the list quietly describe a different job than the detail view.
_JOB_STATUS_PROJECTION = """
    SELECT job_id, root_url, status, created_at, updated_at, stats, error,
           json_array_length(frontier) AS frontier_size,
           COALESCE(json_extract(stats, '$.cap_truncated'), 0) AS cap_truncated
      FROM crawl_jobs
"""


def _status_row(row: sqlite3.Row, visited: int, results: int) -> dict[str, Any]:
    """Shape one projected row into the status view the API returns.

    ``frontier_size`` and ``cap_truncated`` are NULL when the row has no
    frontier or no stats object, which is the normal state of a fresh job.
    """
    return {
        "job_id": row["job_id"],
        "root_url": row["root_url"],
        "status": row["status"],
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
    return json.dumps(value, separators=(",", ":"), default=_json_default)


def _job_row_params(job: CrawlJobState) -> tuple:
    """The live-state columns of the job row, in ``_UPSERT_JOB_ROW_SQL`` order.

    Separate from the statement so the two cannot drift: a column added to the
    SQL without a matching entry here is a TypeError at the first persist
    rather than a silently unwritten column.
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
        _json(job.transient_retries),
    )


class JobAccumulators:
    """The growing part of a job: its page history and its visited set.

    These two collections used to be JSON columns on the job row, so a batch
    persist re-serialized every page crawled so far and total write volume over
    a crawl grew as the square of the page count. They are append-only, so they
    live in their own tables and a batch costs one INSERT per new page.

    They are also everything about a job that is NOT live state, which is what
    makes them separable: the row holds what the runner is doing right now
    (frontier, status, retries) and can be rewritten wholesale each batch
    because it is bounded; this holds what has already happened, and is only
    ever appended to.

    Caller holds the store's lock: these share the store's single connection.
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

        Without this the tables would keep a deleted job's entire crawl with
        nothing pointing at it, which is worse than the leak the old column
        layout could not have: there, deleting the row deleted the history by
        construction.
        """
        self._conn.execute('DELETE FROM "crawl_job_results" WHERE job_id = ?', (job_id,))
        self._conn.execute('DELETE FROM "crawl_job_visited" WHERE job_id = ?', (job_id,))

    def counts_by_job(self) -> dict[str, tuple[int, int]]:
        """``{job_id: (visited, results)}`` for the job list, in one query.

        Two correlated subqueries per job would make the list view cost a round
        trip per job per counter; the primary keys make both counts a single
        grouped scan of the two tables.
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
        """``(visited, results)`` for one job, without reading either list.

        ``count(*)`` over the primary key is an index probe, so this gives the
        same answer ``len()`` gives after a full load for none of the load. It
        exists so a status read can report a size without materializing the
        page history to measure it.
        """
        row = self._conn.execute(
            "SELECT (SELECT count(*) FROM crawl_job_visited WHERE job_id = ?) AS visited,"
            " (SELECT count(*) FROM crawl_job_results WHERE job_id = ?) AS results",
            (job_id, job_id),
        ).fetchone()
        return row["visited"], row["results"]

    def _append_results(self, job: CrawlJobState) -> None:
        """Insert only the pages this batch added.

        The high-water mark is read from the table rather than remembered, so a
        resumed process that never persisted a count in memory still appends
        exactly the pages it has not written. If the history is shorter than
        what is already stored -- normalization dropped pages a previous save
        had written -- the accumulator was not monotone after all, and the
        cheap path would silently skip pages; resync instead.
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
            ((job_id, seq, _json(result)) for seq, result in enumerate(results) if seq >= persisted),
        )

    def _append_visited(self, job: CrawlJobState) -> None:
        """Insert newly visited pages.

        The count check keeps a batch that discovered nothing from re-probing
        every page seen so far; when there is growth the insert is OR IGNORE, so
        a page visited by two branches collapses to one row and the set
        semantics of the accumulator are the table's primary key.
        """
        job_id = job.job_id
        visited = job.visited
        stored = self._conn.execute(
            'SELECT count(*) AS n FROM "crawl_job_visited" WHERE job_id = ?', (job_id,)
        ).fetchone()["n"]
        if len(visited) <= stored:
            return
        self._conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
            ((job_id, url) for url in visited),
        )


def _create_job(
    store: JobStore,
    job_id: str,
    kind: JobKind,
    root_url: str,
    policy: CrawlPolicy,
    frontier: list[FrontierEntry] | None = None,
) -> CrawlJobState:
    """The one place a job is born, for both kinds, and the one place it is written.

    The two kinds used to be built by two nearly-identical blocks inside the
    store, and one of them had already drifted: ``create_single_shot`` carried
    the store's ``host_policy`` but not its ``tier``, so every limit on a
    single-shot job resolved against the model's defaults while a whole-site
    job on the same store resolved against the deployment's. Same request, same
    answer, two numbers.

    A module function rather than a method so the class holds only the two
    one-line calls into it: the row's content and the locked write are one
    decision, and splitting them across the boundary is what let the two kinds
    disagree.
    """
    job = CrawlJobState(
        job_id=job_id,
        kind=kind,
        root_url=root_url,
        policy=_stored_policy(policy),
        status=JobStatus.PENDING,
        frontier=frontier or [],
        host_policy=store._host_policy,
        tier=store.tier,
    )
    with store._lock:
        store._persist(job)
    return job


class JobStore:
    """SQLite-backed store for crawl job states.

    Every method is synchronous SQLite work, so every method has an
    ``a``-prefixed twin that runs the call off the event loop, and any caller
    that CAN reach a loop goes through the twin (``self._lock`` serializes
    every connection use, so a twin never races a direct sync call).
    """

    def __init__(self, db_path: str | Path, *, policy: HostPolicy, tier: PolicyTier):
        self.db_path = Path(db_path)
        self._host_policy = policy
        # The deployment's operator tier, required rather than defaulted: it is
        # what makes a stored row that omitted a limit resolve to the same
        # number the API path would have used, and an optional argument here was
        # how the job path silently started answering with the model defaults
        # instead.
        self.tier = tier
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Durability/concurrency settings come from the shared store
        # chokepoint (busy_timeout + WAL; M1 confinement invariant).
        self._conn = connect_store(self.db_path, schema=StoreSchema.CRAWL_JOBS)
        # Serialize all connection use across threads (sqlite3 connections are
        # not safe for concurrent use even with check_same_thread=False).
        self._lock = threading.Lock()
        # Column-addressed rows: the schema grows by migration, so positional
        # indexing for a later-added column would couple the read path to ALTER
        # ordering. sqlite3.Row still supports row[n].
        self._conn.row_factory = sqlite3.Row
        # The append-only side of a job lives behind its own owner so the row
        # stays a row: live state is rewritten wholesale each batch, history is
        # only ever appended to, and conflating them is what made both cost
        # the same thing.
        self._accumulators = JobAccumulators(self._conn)

    def _row_to_job(self, row) -> CrawlJobState:
        job_id = row["job_id"]
        return CrawlJobState.from_json(
            {
                "job_id": job_id,
                "kind": row["kind"],
                "root_url": row["root_url"],
                "policy": json.loads(row["policy"]),
                "status": row["status"],
                "frontier": json.loads(row["frontier"]),
                "visited": self._accumulators.read_visited(job_id),
                "results": self._accumulators.read_results(job_id),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "error": row["error"],
                "stats": json.loads(row["stats"]),
                "in_flight": json.loads(row["in_flight"]),
                "transient_retries": json.loads(row["transient_retries"]),
            },
            host_policy=self._host_policy,
            tier=self.tier,
        )

    def create(self, root_url: str, policy: CrawlPolicy) -> CrawlJobState:
        """Create a new whole-site crawl job with initial frontier containing root_url."""
        # Full UUID hex (32 chars) to avoid brute-forceable short IDs (HIGH #9).
        canonical_root = str(normalize_public_http_url(root_url, policy=self._host_policy))
        return _create_job(
            self,
            new_job_id(),
            JobKind.WHOLE_SITE,
            canonical_root,
            read_policy(policy, self.tier),
            frontier=[FrontierEntry(url=canonical_root, depth=0)],
        )

    def create_single_shot(self, job_id: str, root_url: str, policy: CrawlPolicy) -> CrawlJobState:
        """Create a new single-shot async crawl job with the given job_id (request_id)."""
        return _create_job(
            self,
            job_id,
            JobKind.SINGLE_SHOT,
            str(normalize_public_http_url(root_url, policy=self._host_policy)),
            read_policy(policy, self.tier),
        )

    def save(self, job: CrawlJobState) -> None:
        """Persist job DATA to SQLite (thread-safe).

        ``status`` is written only on first INSERT (a new row's initial
        state); the UPDATE path deliberately never touches it. Control state
        moves exclusively through :meth:`set_status` (H3): a whole-row save
        from a stale snapshot must not resurrect the writer's status view
        and clobber a concurrent pause/cancel.
        """
        with self._lock:
            self._persist(job)

    async def asave(self, job: CrawlJobState) -> None:
        await asyncio.to_thread(self.save, job)

    async def acreate(self, root_url: str, policy: CrawlPolicy) -> CrawlJobState:
        return await asyncio.to_thread(self.create, root_url, policy)

    async def acreate_single_shot(self, job_id: str, root_url: str, policy: CrawlPolicy) -> CrawlJobState:
        return await asyncio.to_thread(self.create_single_shot, job_id, root_url, policy)

    async def aset_status(self, job_id: str, new: JobStatus, *, expected: _Expected) -> bool:
        return await asyncio.to_thread(self.set_status, job_id, new, expected=expected)

    async def aload(self, job_id: str) -> CrawlJobState | None:
        return await asyncio.to_thread(self.load, job_id)

    async def adelete(self, job_id: str) -> bool:
        return await asyncio.to_thread(self.delete, job_id)

    async def alist_jobs(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_jobs)

    async def astatus(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.status_row, job_id)

    async def aexists(self, job_id: str) -> bool:
        return await asyncio.to_thread(self.exists, job_id)

    def _persist(self, job: CrawlJobState) -> None:
        """Write job state to SQLite. Caller must hold ``self._lock``.

        No normalization pass: identity is established at ingress, and the read
        boundary in :meth:`from_json` re-derives it once per load. Re-deriving
        every URL here instead would multiply the job loop's cost by the whole
        page history on every batch persist.

        The row carries live state only. ``results`` and ``visited`` are
        append-only accumulators in their own tables, so a batch costs one
        INSERT per new page instead of re-serializing every page crawled so
        far -- the difference between O(pages) and O(1) per batch.
        """
        job.updated_at = time.time()
        self._conn.execute(_UPSERT_JOB_ROW_SQL, _job_row_params(job))
        self._accumulators.append(job)
        self._conn.commit()

    def set_status(self, job_id: str, new: JobStatus, *, expected: _Expected) -> bool:
        """Atomic compare-and-set on the row's status (the control plane).

        Returns True iff the row existed in one of the ``expected`` states
        and now holds ``new``. False means missing-or-conflict: the caller's
        view of the job is stale, and the persisted control state stands --
        never silently overwritten by whichever writer saved last.
        """
        wanted = {expected} if isinstance(expected, JobStatus) else set(expected)
        with self._lock:
            # json_each keeps the expected set a BOUND parameter -- no SQL is
            # ever constructed from values (and no S608-shaped f-string).
            cur = self._conn.execute(
                "UPDATE crawl_jobs SET status = ?, updated_at = ? "
                "WHERE job_id = ? AND status IN (SELECT value FROM json_each(?))",
                (new.value, time.time(), job_id, json.dumps(sorted(s.value for s in wanted))),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def load(self, job_id: str) -> CrawlJobState | None:
        """Load job state from SQLite. Returns None if not found.

        The whole read -- row, page history, visited set -- happens under one
        hold of the lock, and it has to. Deserializing the state is no longer
        free: the history and the visited set live in their own tables, so it is
        two more queries on the shared connection. Releasing the lock between
        them would let a writer interleave, and two things would break at once:
        the connection would be driven from two threads (an
        ``InterfaceError``, which surfaces as a live job reading as missing and
        the runner dying on it), and the row's ``seq`` keys would be read
        against a visited set from a different point in time, so a load could
        return a state that never existed.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, kind, root_url, policy, status, frontier, "
                "created_at, updated_at, error, stats, in_flight, transient_retries "
                "FROM crawl_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                return self._row_to_job(row)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                # Surface corruption instead of silently returning None so a
                # broken row is observable in logs rather than masked.
                logger.warning("job_store_load_corrupt", extra={"job_id": job_id, "error": str(exc)})
                return None

    def delete(self, job_id: str) -> bool:
        """Delete job from SQLite. Returns True if existed.

        The page history and the visited set go with it. They are child tables
        rather than columns, so a delete that only removed the row would leave
        a job's entire crawl behind with nothing pointing at it.
        """
        with self._lock:
            self._accumulators.delete(job_id)
            cur = self._conn.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def list_jobs(self) -> list[dict[str, Any]]:
        """Every job's status view, newest first."""
        with self._lock:
            counts = self._accumulators.counts_by_job()
            rows = self._conn.execute(_JOB_STATUS_PROJECTION + " ORDER BY created_at DESC").fetchall()
        return [_status_row(r, *counts.get(r["job_id"], (0, 0))) for r in rows]

    def status_row(self, job_id: str) -> dict[str, Any] | None:
        """The status view for one job, or None if there is no such job.

        Deliberately not :meth:`load`. The response exposes counts, and loading
        the state in order to count it deserializes every result payload and
        every visited URL only for the caller to throw them away after ``len()``.
        On a long job that is the whole crawl, per call, on the endpoint a
        client polls while its crawl runs.
        """
        with self._lock:
            row = self._conn.execute(_JOB_STATUS_PROJECTION + " WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            visited, results = self._accumulators.counts_for(job_id)
        return _status_row(row, visited, results)

    def exists(self, job_id: str) -> bool:
        """Whether a job row exists, without reading it.

        For the control plane's existence check, which is a gate rather than a
        question about the job: the answer it needs is whether the row is there,
        so anything that reads the row to answer it is doing more than asked.
        """
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM crawl_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row is not None

    def close(self) -> None:
        """Close the database connection (thread-safe)."""
        with self._lock:
            self._conn.close()
