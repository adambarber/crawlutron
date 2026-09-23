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

logger = logging.getLogger(__name__)
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.crawl.ids import new_job_id
from app.crawl.migrations import StoreSchema
from app.crawl.sqlite_store import connect_store
from app.validation import safe_public_url_or_none


def _json_default(obj):
    """JobStore save fallback -- now the shared app.json_safe chokepoint."""
    from app.json_safe import json_default

    return json_default(obj)


class JobKind(str, Enum):
    """Kind of crawl job."""

    WHOLE_SITE = "whole_site"  # Recursive crawl with frontier/visited
    SINGLE_SHOT = "single_shot"  # Single async crawl (no frontier/visited)


class JobStatus(str, Enum):
    """Lifecycle status of a crawl job."""

    PENDING = "pending"  # Created, not yet started
    RUNNING = "running"  # Actively crawling
    PAUSED = "paused"  # Paused by user
    COMPLETED = "completed"  # Frontier exhausted / crawl finished
    FAILED = "failed"  # Fatal error
    CANCELLED = "cancelled"  # Explicitly cancelled


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
def _canonical_cached(url: str) -> str:
    return str(safe_public_url_or_none(url) or url)


def _canonical_or_raw(url: str) -> str:
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
    return _canonical_cached(url)


def normalized_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}
    normalized = dict(policy)
    if "limits" in normalized and not isinstance(normalized["limits"], dict):
        normalized["limits"] = {}
    return normalized


def policy_limits(policy: Any) -> dict[str, Any]:
    limits = normalized_policy(policy).get("limits")
    return dict(limits) if isinstance(limits, dict) else {}


def job_max_depth(policy: Any) -> int:
    value = policy_limits(policy).get("max_depth")
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def job_max_pages(policy: Any) -> int:
    value = policy_limits(policy).get("max_pages")
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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


def _row_is_canonical(*collections: Any) -> bool:
    """Whether persisted collections already hold ``{url, depth}`` records.

    Canonical form is read from the DATA, never from a stored flag: a flag can
    disagree with the row it claims to describe (a direct SQL write, a migration,
    an older writer), and then the read path would skip normalization on a row
    that needs it. Every writer in this codebase emits records, and a legacy row
    holds bare strings, so the shape is the honest signal -- and it costs one
    isinstance check per entry instead of a URL parse.
    """
    for values in collections:
        if not isinstance(values, (list, tuple)):
            return False
        for value in values:
            if not isinstance(value, dict) or "url" not in value or "depth" not in value:
                return False
    return True


def normalize_frontier(
    values: Any,
    *,
    root_url: str,
    policy: dict[str, Any],
    canonicalize: bool = True,
) -> list[FrontierEntry]:
    """Coerce persisted/runner-supplied frontier values into ``FrontierEntry`` records.

    ``canonicalize=False`` is the steady-state read path for rows this writer
    already stored in canonical form (the persisted ``canonicalized`` marker):
    it still coerces shape, keeps the shallowest depth per identity, and drops
    unusable entries, but skips the per-URL canonicalization work. Skipping it
    on EVERY load/save instead cost 65-146x the pre-normalization persistence
    time and stalled the event loop for a quarter second on a 20k-entry job.
    """
    if not isinstance(values, (list, tuple)):
        return []
    canonical_root = _canonical_or_raw(root_url) if canonicalize else root_url
    legacy_depth = job_max_depth(policy)
    entries: dict[str, FrontierEntry] = {}
    order: list[str] = []
    for value in values:
        raw_url: Any
        depth: Any
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
        url = _canonical_or_raw(raw_url) if canonicalize else raw_url
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
    policy: dict[str, Any],
    visited: Any,
    frontier: Any,
    in_flight: Any,
) -> tuple[set[str], list[FrontierEntry], list[FrontierEntry]]:
    canonical_root = _canonical_or_raw(root_url)
    normalized_policy_value = normalized_policy(policy)
    if isinstance(visited, str):
        visited_values = [visited]
    else:
        try:
            visited_values = list(visited or [])
        except TypeError:
            visited_values = []
    normalized_visited = {
        _canonical_or_raw(url)
        for url in visited_values
        if isinstance(url, str) and url
    }
    normalized_frontier = normalize_frontier(
        frontier, root_url=canonical_root, policy=normalized_policy_value
    )
    normalized_in_flight = normalize_frontier(
        in_flight, root_url=canonical_root, policy=normalized_policy_value
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
        data["frontier"] = [_entry_payload(entry) for entry in self.frontier]
        data["in_flight"] = [_entry_payload(entry) for entry in self.in_flight]
        data["visited"] = sorted(self.visited)
        data["stats"] = _normalized_stats(self.stats)
        data["status"] = self.status.value
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CrawlJobState:
        """Deserialize from JSON dict.

        The read boundary always normalizes: canonical identity, cross-list
        dedupe, shallowest depth and legacy bare-string depths are enforced
        here, so a row written by any writer -- including one that put
        non-canonical URLs inside well-formed records -- cannot reintroduce a
        second identity for a page. The cost is bounded by the canonicalization
        memo in :func:`_canonical_or_raw` rather than by skipping the work.
        """
        canonicalized = False
        status = data.get("status", JobStatus.PENDING.value)
        kind = data.get("kind", JobKind.WHOLE_SITE.value)
        policy = normalized_policy(data.get("policy"))
        root_url = str(data.get("root_url") or "")
        if not canonicalized:
            root_url = _canonical_or_raw(root_url)
        visited_raw = data.get("visited", [])
        if isinstance(visited_raw, list):
            visited = {
                (url if canonicalized else _canonical_or_raw(url))
                for url in visited_raw
                if isinstance(url, str) and url
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
                    key = url if canonicalized else _canonical_or_raw(url)
                    transient_retries[key] = max(0, int(attempts))
                except (TypeError, ValueError):
                    continue
        job = cls(
            job_id=str(data.get("job_id") or ""),
            kind=JobKind(kind),
            root_url=root_url,
            policy=policy,
            status=JobStatus(status),
            frontier=normalize_frontier(
                data.get("frontier", []),
                root_url=root_url,
                policy=policy,
                canonicalize=not canonicalized,
            ),
            visited=visited,
            in_flight=normalize_frontier(
                data.get("in_flight", []),
                root_url=root_url,
                policy=policy,
                canonicalize=not canonicalized,
            ),
            transient_retries=transient_retries,
            results=list(data.get("results", [])) if isinstance(data.get("results", []), list) else [],
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            error=data.get("error") if isinstance(data.get("error"), str) else None,
            stats=_normalized_stats(data.get("stats", {})),
        )
        if not canonicalized:
            # Legacy row: the one place full normalization is owed. The marker is
            # set on the next save, so this cost is paid once per legacy row.
            normalize_job_state(job)
        return job


def normalize_job_state(job: CrawlJobState) -> CrawlJobState:
    root_url = _canonical_or_raw(job.root_url)
    policy = normalized_policy(job.policy)
    visited, frontier, in_flight = normalize_job_collections(
        root_url=root_url,
        policy=policy,
        visited=job.visited,
        frontier=job.frontier,
        in_flight=job.in_flight,
    )
    transient_retries: dict[str, int] = {}
    for url, attempts in job.transient_retries.items():
        if not isinstance(url, str) or not url:
            continue
        try:
            value = max(0, int(attempts))
        except (TypeError, ValueError):
            continue
        identity = _canonical_or_raw(url)
        transient_retries[identity] = max(transient_retries.get(identity, 0), value)
    job.root_url = root_url
    job.policy = policy
    job.visited = visited
    job.frontier = frontier
    job.in_flight = in_flight
    job.transient_retries = transient_retries
    job.stats = _normalized_stats(job.stats)
    return job


class JobStore:
    """SQLite-backed store for crawl job states."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Durability/concurrency settings come from the shared store
        # chokepoint (busy_timeout + WAL; M1 confinement invariant).
        self._conn = connect_store(self.db_path, schema=StoreSchema.CRAWL_JOBS)
        # Serialize all connection use across threads (sqlite3 connections are
        # not safe for concurrent use even with check_same_thread=False).
        self._lock = threading.Lock()
        # Column-addressed rows: the schema grows by migration, so positional
        # indexing for columns added later (e.g. canonicalized) would couple the
        # read path to ALTER ordering. sqlite3.Row still supports row[n].
        self._conn.row_factory = sqlite3.Row

    def _row_to_job(self, row) -> CrawlJobState:
        return CrawlJobState.from_json(
            {
                "job_id": row[0],
                "kind": row[1],
                "root_url": row[2],
                "policy": json.loads(row[3]),
                "status": row[4],
                "frontier": json.loads(row[5]),
                "visited": json.loads(row[6]),
                "results": json.loads(row[7]),
                "created_at": row[8],
                "updated_at": row[9],
                "error": row[10],
                "stats": json.loads(row[11]),
                "in_flight": json.loads(row[12]),
                "transient_retries": json.loads(row[13]),
            }
        )

    def create(self, root_url: str, policy: dict[str, Any]) -> CrawlJobState:
        """Create a new whole-site crawl job with initial frontier containing root_url."""
        # Full UUID hex (32 chars) to avoid brute-forceable short IDs (HIGH #9).
        job_id = new_job_id()
        canonical_root = _canonical_or_raw(root_url)
        normalized = normalized_policy(policy)
        job = CrawlJobState(
            job_id=job_id,
            kind=JobKind.WHOLE_SITE,
            root_url=canonical_root,
            policy=normalized,
            status=JobStatus.PENDING,
            frontier=[FrontierEntry(url=canonical_root, depth=0)],
        )
        with self._lock:
            self._persist(job)
        return job

    def create_single_shot(self, job_id: str, root_url: str, policy: dict[str, Any]) -> CrawlJobState:
        """Create a new single-shot async crawl job with the given job_id (request_id)."""
        job = CrawlJobState(
            job_id=job_id,
            kind=JobKind.SINGLE_SHOT,
            root_url=_canonical_or_raw(root_url),
            policy=normalized_policy(policy),
            status=JobStatus.PENDING,
        )
        with self._lock:
            self._persist(job)
        return job

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
        """Async :meth:`save`: offload the whole row -- the ``json.dumps`` of
        results, visited and frontier plus the SQLite commit -- to a worker
        thread so the event loop is never stalled.

        Synchronous callers must keep using :meth:`save`: startup recovery
        (``recover_orphaned_running_jobs``) is sync by design and runs before a
        loop exists. This is the same sibling shape ``ContentCache`` uses for
        ``set``/``aset``, and it keeps the offload discipline inside the store
        rather than at each call site, so a new async caller cannot forget it
        the way ``run_async_crawl`` did.

        ``self._lock`` serializes every connection use, so a save run here is
        safe against a concurrent sync ``save`` from the startup/recovery path.
        """
        await asyncio.to_thread(self.save, job)

    def _persist(self, job: CrawlJobState) -> None:
        """Write job state to SQLite. Caller must hold ``self._lock``.

        No normalization pass: identity is established at ingress and the row is
        written with the ``canonicalized`` marker, so re-deriving every URL here
        would multiply the job loop's cost by the whole page history on every
        batch persist. Legacy rows are normalized once when they are read.
        """
        job.updated_at = time.time()
        root_url = job.root_url
        policy = job.policy
        frontier = job.frontier
        in_flight = job.in_flight
        visited = job.visited
        stats = job.stats
        transient_retries = job.transient_retries
        self._conn.execute(
            """
                  INSERT INTO crawl_jobs (job_id, kind, root_url, policy, status, frontier, visited, results,
                                          created_at, updated_at, error, stats, in_flight, transient_retries)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                kind=excluded.kind,
                root_url=excluded.root_url,
                policy=excluded.policy,
                frontier=excluded.frontier,
                visited=excluded.visited,
                results=excluded.results,
                updated_at=excluded.updated_at,
                error=excluded.error,
                      stats=excluded.stats,
                      in_flight=excluded.in_flight,
                      transient_retries=excluded.transient_retries
              """,
            (
                job.job_id,
                job.kind.value,
                root_url,
                json.dumps(policy, separators=(",", ":"), default=_json_default),
                job.status.value,
                json.dumps(
                    [_entry_payload(entry) for entry in frontier],
                    separators=(",", ":"),
                    default=_json_default,
                ),
                json.dumps(list(visited), separators=(",", ":"), default=_json_default),
                json.dumps(job.results, separators=(",", ":"), default=_json_default),
                job.created_at,
                job.updated_at,
                job.error,
                json.dumps(stats, separators=(",", ":"), default=_json_default),
                json.dumps(
                    [_entry_payload(entry) for entry in in_flight],
                    separators=(",", ":"),
                    default=_json_default,
                ),
                json.dumps(transient_retries, separators=(",", ":"), default=_json_default),
            ),
        )
        self._conn.commit()

    def set_status(
        self,
        job_id: str,
        new: JobStatus,
        *,
        expected: JobStatus | frozenset[JobStatus],
    ) -> bool:
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
        """Load job state from SQLite. Returns None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, kind, root_url, policy, status, frontier, visited, results, "
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
        """Delete job from SQLite. Returns True if existed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs with basic info (no frontier/visited/results)."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT job_id, root_url, status, kind, created_at, updated_at,
                       json_array_length(frontier) as frontier_size,
                       json_array_length(visited) as visited_size,
                       json_array_length(results) as results_count,
                       COALESCE(json_extract(stats, '$.cap_truncated'), 0) as cap_truncated
                FROM crawl_jobs
                ORDER BY created_at DESC
            """).fetchall()
        return [
            {
                "job_id": r[0],
                "root_url": r[1],
                "status": r[2],
                "kind": r[3],
                "created_at": r[4],
                "updated_at": r[5],
                "frontier_size": r[6] or 0,
                "visited_size": r[7] or 0,
                "results_count": r[8] or 0,
                "cap_truncated": bool(r[9]),
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection (thread-safe)."""
        with self._lock:
            self._conn.close()
