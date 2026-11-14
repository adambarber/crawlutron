"""Job store: the single connection and lock over crawl-job persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

from crawlutron.admission.policy import PolicyLike, PolicyTier, read_policy
from crawlutron.storage.coordination_store import atwin
from crawlutron.storage.ids import new_job_id
from crawlutron.storage.migrations import StoreSchema
from crawlutron.storage.sqlite_store import (
    bind_conn_lifecycle,
    connect_store,
    connection_data_version,
    process_lock,
    store_lock_key,
)

from . import job_store_lease as _lease
from .job_store_accumulators import JobAccumulators
from .job_store_collections import (
    TERMINAL_STATUSES,
    Expected,
    FrontierEntry,
    JobKind,
    JobStatus,
    OrderedUrlSet,
    job_max_pages,
)
from .job_store_lease import DEFAULT_JOB_CLAIM_SECONDS
from .job_store_read import (
    _JOB_LIST_ORDER,
    _JOB_STATUS_PROJECTION,
    JobStatusView,
    _status_row,
    normalize_job_state,
    row_to_state,
)
from .job_store_state import (
    _UPSERT_JOB_ROW_SQL,
    CrawlJobState,
    _create_job,
    _job_row_params,
)

_ROW_COLUMNS = (
    "SELECT job_id, kind, owner, root_url, policy, status, created_at, updated_at, error, "
    "stats, in_flight, transient_retries, claimed_by, claimed_at FROM crawl_jobs"
)


def _owner_statement(
    projection: str, where_job_id: bool, tail: str, owner: str | None
) -> tuple[str, tuple[object, ...]]:
    """The owner-filtered read statement and its bindings, every fragment a
    module literal (``""`` is a real owner, so the split is on None alone)."""
    anchor = " WHERE job_id = ?" if where_job_id else " WHERE 1=1"
    owner_clause = "" if owner is None else " AND owner = ?"
    statement = projection + anchor + owner_clause + " AND deleted_at IS NULL" + tail
    return statement, () if owner is None else (owner,)


def _fetch_owner_row(
    conn: sqlite3.Connection, statement: str, params: tuple[object, ...]
) -> sqlite3.Row | None:
    """One owner-filtered row fetch: execute and fetchone, None on miss.
    The idiom the read sites share; each site owns its own transaction posture."""
    return conn.execute(statement, params).fetchone()


logger = logging.getLogger(__name__)

#: The admitted batch seeded at a single-shot job's birth (None = single URL).
type _Batch = list[FrontierEntry] | None


# set_status SQL is built in job_store_lease for file-size budget.


def _read_job_row(
    store: JobStore, job_id: str, *, with_accumulators: bool, owner: str | None = None
) -> CrawlJobState | None:
    statement, params = _owner_statement(_ROW_COLUMNS, True, "", owner)
    try:
        with store._lock:
            store._conn.execute("BEGIN")
            try:
                row = _fetch_owner_row(store._conn, statement, (job_id, *params))
                if row is None:
                    store._conn.execute("COMMIT")
                    return None
                factory = store._row_to_job_factory
                if factory is None:
                    state = store._read_collections(row, with_accumulators=with_accumulators)
                else:
                    state = factory(row, store._row_to_job, with_accumulators=with_accumulators)
            except BaseException:
                store._conn.execute("ROLLBACK")
                raise
            store._conn.execute("COMMIT")
            return state
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("job_store_load_corrupt", extra={"job_id": job_id, "error": str(exc)})
        return None


__all__ = [
    "DEFAULT_JOB_CLAIM_SECONDS",
    "TERMINAL_STATUSES",
    "CrawlJobState",
    "FrontierEntry",
    "JobKind",
    "JobStatus",
    "JobStatusView",
    "JobStore",
    "Expected",
    "OrderedUrlSet",
    "job_max_pages",
    "normalize_job_state",
]


class JobStore:
    """SQLite-backed store for crawl job states.
    Every method is synchronous SQLite work, so each has an ``a``-prefixed twin
    running off the loop; ``self._lock`` serializes every connection use.
    """

    _conn: sqlite3.Connection

    def __init__(
        self,
        db_path: str | Path,
        *,
        policy: HostPolicy,
        tier: PolicyTier,
        row_to_job: Callable[..., CrawlJobState] | None = None,
    ):
        self.db_path = Path(db_path)
        self._host_policy = policy
        # Operator tier: an omitted limit resolves to the API path's number, not the default.
        self.tier = tier
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn, memory_name = connect_store(self.db_path, schema=StoreSchema.CRAWL_JOBS)
        # Serialize all connection use: sqlite3 connections are unsafe across
        # threads, and every other store instance and migration touches this file.
        self.lock_key = store_lock_key(self.db_path, memory_name)
        self._lock = process_lock(self.lock_key)
        # Column-addressed rows: the schema grows by migration, so positional
        # indexing would couple reads to ALTER ordering.
        self._conn.row_factory = sqlite3.Row
        # A store whose last reference dies closes its connection at that GC point (see bind_conn_lifecycle).
        bind_conn_lifecycle(self)
        # A caller-supplied row-to-model mapping replaces the default for
        # observation or a custom projection; it runs under the same lock.
        self._row_to_job_factory: Callable[..., CrawlJobState] | None = row_to_job
        # Live state is rewritten wholesale each batch, history only appended;
        # the append-only side has its own owner so neither pays for the other.
        self._accumulators = JobAccumulators(self._conn)

    def _read_collections(self, row: sqlite3.Row, *, with_accumulators: bool) -> CrawlJobState:
        """Collections read under the (reentrant) lock; the caller owns the hold.

        The single body both loaders share, so the atomic hold cannot drift.
        """
        with self._lock:
            visited = self._accumulators.read_visited(row["job_id"]) if with_accumulators else []
            results = self._accumulators.read_results(row["job_id"]) if with_accumulators else []
            frontier = self._accumulators.read_frontier(row["job_id"])
        return row_to_state(
            row,
            visited=visited,
            results=results,
            frontier=frontier,
            host_policy=self._host_policy,
            tier=self.tier,
        )

    def _row_to_job(self, row: sqlite3.Row, *, with_accumulators: bool = True) -> CrawlJobState:
        """One row, read through the declared record (the walk lives beside it).

        Entered from ``_read_row`` the caller's hold spans reads and walk; a direct
        entry re-acquires for the reads only. The frontier always loads.
        """
        return self._read_collections(row, with_accumulators=with_accumulators)

    def create(self, root_url: str, policy: PolicyLike, *, owner: str = "") -> CrawlJobState:
        canonical_root = NormalizedUrl(root_url, policy=self._host_policy)
        return _create_job(
            self,
            new_job_id(),
            JobKind.WHOLE_SITE,
            canonical_root.raw,
            read_policy(policy, self.tier),
            frontier=[FrontierEntry(url=canonical_root, depth=0)],
            owner=owner,
        )

    def create_single_shot(
        self, job_id: str, root_url: str, policy: PolicyLike, *, owner: str = "", frontier: _Batch = None
    ) -> CrawlJobState:
        return _create_job(
            self,
            job_id,
            JobKind.SINGLE_SHOT,
            str(NormalizedUrl(root_url, policy=self._host_policy)),
            read_policy(policy, self.tier),
            frontier=frontier,
            owner=owner,
        )

    def save(self, job: CrawlJobState) -> None:
        """Persist job DATA to SQLite (thread-safe). ``status`` and ``owner`` are
        INSERT-only -- the UPDATE path never touches them -- so a stale whole-row
        save cannot clobber a pause/cancel or transfer ownership."""
        with self._lock:
            self._persist(job)

    def _persist(self, job: CrawlJobState) -> None:
        """Write job state to SQLite. Caller must hold ``self._lock``.
        No normalization pass: identity is established at ingress. A tombstoned
        (deleted) job is a no-op here, so a late ``save`` cannot resurrect it."""
        if self._is_tombstoned(job.job_id):
            return
        self._accumulators._seed_table_truth(job.job_id)
        # Capture BEFORE our write: a sibling committing during or after this save
        # bumps data_version past it, so the next save cannot mistake us for last.
        version = connection_data_version(self._conn)
        job.updated_at = time.time()
        self._conn.execute(_UPSERT_JOB_ROW_SQL, _job_row_params(job))
        self._accumulators.append(job)
        self._conn.commit()
        # Stamped only after the write paths ran, so the proof reflects our content.
        self._accumulators._seed_version(job.job_id, version)

    def set_status(
        self,
        job_id: str,
        new: JobStatus,
        *,
        expected: Expected,
        error: str | None = None,
        claimed_by: str | None = None,
        claim_now: float | None = None,
        clear_claim: bool = False,
        require_owner: bool = False,
        claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
    ) -> bool:
        return _lease.set_status(
            self._conn,
            self._lock,
            job_id,
            new,
            expected=expected,
            error=error,
            claimed_by=claimed_by,
            claim_now=claim_now,
            claim_seconds=claim_seconds,
            require_owner=require_owner,
            clear_claim=clear_claim,
        )

    def load(self, job_id: str) -> CrawlJobState | None:
        return self._read_row(job_id, with_accumulators=True)

    def reload_state(self, job_id: str) -> CrawlJobState | None:
        return self._read_row(job_id, with_accumulators=False)

    def _read_row(
        self, job_id: str, *, with_accumulators: bool, owner: str | None = None
    ) -> CrawlJobState | None:
        return _read_job_row(self, job_id, with_accumulators=with_accumulators, owner=owner)

    def load_for_caller(self, job_id: str, caller_id: str) -> CrawlJobState | None:
        return self._read_row(job_id, with_accumulators=True, owner=caller_id)

    def delete(self, job_id: str) -> bool:
        return self._tombstone_row(
            job_id,
            "UPDATE crawl_jobs SET deleted_at = ?, updated_at = ? WHERE job_id = ? AND deleted_at IS NULL",
            (job_id,),
        )

    def delete_for_caller(self, job_id: str, caller_id: str) -> bool:
        return self._tombstone_row(
            job_id,
            "UPDATE crawl_jobs SET deleted_at = ?, updated_at = ? "
            "WHERE job_id = ? AND owner = ? AND deleted_at IS NULL",
            (job_id, caller_id),
        )

    def _tombstone_row(self, job_id: str, statement: str, params: tuple[object, ...]) -> bool:
        with self._lock:
            if self._is_tombstoned(job_id):
                return False
            cur = self._conn.execute(statement, (time.time(), time.time(), *params))
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._accumulators.delete(job_id)
            self._conn.commit()
        return True

    def _is_tombstoned(self, job_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM crawl_jobs WHERE job_id = ? AND deleted_at IS NOT NULL", (job_id,)
        ).fetchone()
        return row is not None

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[JobStatusView]:
        return self._read_job_list(None, limit=limit, offset=offset)

    def list_for_caller(
        self, caller_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[JobStatusView]:
        return self._read_job_list(caller_id, limit=limit, offset=offset)

    def _read_job_list(self, owner: str | None, *, limit: int | None, offset: int = 0) -> list[JobStatusView]:
        with self._lock:
            counts = self._accumulators.counts_by_job()
            statement, params = _owner_statement(_JOB_STATUS_PROJECTION, False, _JOB_LIST_ORDER, owner)
            if limit is not None:
                statement += " LIMIT ? OFFSET ?"
                params = (*params, max(0, limit), max(0, offset))
            rows = self._conn.execute(statement, params).fetchall()
        return [_status_row(r, *counts.get(r["job_id"], (0, 0))) for r in rows]

    def count_jobs(self) -> int:
        return self._count_jobs(None)

    def count_for_caller(self, caller_id: str) -> int:
        return self._count_jobs(caller_id)

    def _count_jobs(self, owner: str | None) -> int:
        with self._lock:
            statement, params = _owner_statement("SELECT count(*) FROM crawl_jobs", False, "", owner)
            row = self._conn.execute(statement, params).fetchone()
        return int(row[0])

    def status_row(self, job_id: str) -> JobStatusView | None:
        return self._read_status_row(job_id, None)

    def status_for_caller(self, job_id: str, caller_id: str) -> JobStatusView | None:
        return self._read_status_row(job_id, caller_id)

    def _read_status_row(self, job_id: str, owner: str | None) -> JobStatusView | None:
        with self._lock:
            statement, params = _owner_statement(_JOB_STATUS_PROJECTION, True, "", owner)
            row = _fetch_owner_row(self._conn, statement, (job_id, *params))
            if row is None:
                return None
            visited, results = self._accumulators.counts_for(job_id)
        return _status_row(row, visited, results)

    def exists(self, job_id: str) -> bool:
        return self._has_job_row(job_id, None)

    def exists_for_caller(self, job_id: str, caller_id: str) -> bool:
        return self._has_job_row(job_id, caller_id)

    def active_count_for_caller(self, caller_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) FROM crawl_jobs WHERE owner = ? AND status IN (?, ?) AND deleted_at IS NULL",
                (caller_id, JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchone()
        return int(row[0])

    def _has_job_row(self, job_id: str, owner: str | None) -> bool:
        with self._lock:
            statement, params = _owner_statement("SELECT 1 FROM crawl_jobs", True, "", owner)
            row = _fetch_owner_row(self._conn, statement, (job_id, *params))
        return row is not None

    def claim_running(
        self, job_id: str, token: str, *, now: float, claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS
    ) -> bool:
        return _lease.claim_running(
            self._conn, self._lock, job_id, token, now=now, claim_seconds=claim_seconds
        )

    def renew_job_claim(self, job_id: str, token: str, *, now: float) -> bool:
        return _lease.renew_job_claim(self._conn, self._lock, job_id, token, now=now)

    def release_job_claim(self, job_id: str, token: str) -> bool:
        return _lease.release_job_claim(self._conn, self._lock, job_id, token)

    def clear_job_claim(self, job_id: str) -> bool:
        return _lease.clear_job_claim(self._conn, self._lock, job_id)

    def running_claim(self, job_id: str) -> tuple[str | None, float | None, str | None] | None:
        return _lease.running_claim(self._conn, self._lock, job_id)

    def is_job_claim_fresh(
        self, job_id: str, *, now: float, claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS
    ) -> bool:
        return _lease.is_job_claim_fresh(self._conn, self._lock, job_id, now=now, claim_seconds=claim_seconds)

    def retire_orphan_running(
        self,
        job_id: str,
        new: JobStatus,
        *,
        now: float,
        error: str | None = None,
        claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
    ) -> bool:
        return _lease.retire_orphan_running(
            self._conn,
            self._lock,
            job_id,
            new,
            now=now,
            claim_seconds=claim_seconds,
            error=error,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Async twins for the event loop.
    asave = atwin(save)
    acreate = atwin(create)
    acreate_single_shot = atwin(create_single_shot)
    aset_status = atwin(set_status)
    aload = atwin(load)
    aload_for_caller = atwin(load_for_caller)
    areload_state = atwin(reload_state)
    adelete = atwin(delete)
    adelete_for_caller = atwin(delete_for_caller)
    alist_jobs = atwin(list_jobs)
    alist_for_caller = atwin(list_for_caller)
    acount_jobs = atwin(count_jobs)
    acount_for_caller = atwin(count_for_caller)
    astatus = atwin(status_row)
    astatus_for_caller = atwin(status_for_caller)
    aexists = atwin(exists)
    aexists_for_caller = atwin(exists_for_caller)
    aactive_count_for_caller = atwin(active_count_for_caller)
    aclaim_running = atwin(claim_running)
    arenew_job_claim = atwin(renew_job_claim)
    arelease_job_claim = atwin(release_job_claim)
    aclear_job_claim = atwin(clear_job_claim)
    arunning_claim = atwin(running_claim)
    ais_job_claim_fresh = atwin(is_job_claim_fresh)
    aretire_orphan_running = atwin(retire_orphan_running)
