"""Job store: the single connection and lock over crawl-job persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from crawlutron.admission.policy import PolicyLike, PolicyTier, read_policy
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.storage.coordination_store import atwin
from crawlutron.storage.ids import new_job_id
from crawlutron.storage.migrations import StoreSchema
from crawlutron.storage.sqlite_store import bind_conn_lifecycle, connect_store, process_lock, store_lock_key

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
from .job_store_read import (
    _JOB_LIST_ORDER,
    _JOB_STATUS_PROJECTION,
    JobStatusView,
    _status_row,
    normalize_job_state,
    row_to_state,
)
from .job_store_state import (
    _CAS_STATUS_SQL,
    _UPSERT_JOB_ROW_SQL,
    CrawlJobState,
    _create_job,
    _job_row_params,
)

_ROW_COLUMNS = (
    "SELECT job_id, kind, owner, root_url, policy, status, created_at, updated_at, error, "
    "stats, in_flight, transient_retries FROM crawl_jobs"
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

__all__ = [
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
        """Create a new whole-site crawl job with initial frontier containing root_url."""
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
        """Create a single-shot async crawl job; ``frontier`` seeds the admitted batch."""
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
        job.updated_at = time.time()
        self._conn.execute(_UPSERT_JOB_ROW_SQL, _job_row_params(job))
        self._accumulators.append(job)
        self._conn.commit()

    def set_status(
        self, job_id: str, new: JobStatus, *, expected: Expected, error: str | None = None
    ) -> bool:
        """Atomic compare-and-set on the row's status (the control plane).
        A tombstoned row is never CASed; ``error`` rides the same UPDATE
        as the status, so the stamp lands only when the CAS does.
        """
        wanted = {expected} if isinstance(expected, JobStatus) else set(expected)
        with self._lock:
            cur = self._conn.execute(
                _CAS_STATUS_SQL,
                (
                    new.value,
                    time.time(),
                    error,
                    job_id,
                    json.dumps(sorted(s.value for s in wanted)),
                ),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def load(self, job_id: str) -> CrawlJobState | None:
        """Load the whole job: live state plus page history and visited set.
        Live state only? Use ``reload_state``; don't pay for the accumulators."""
        return self._read_row(job_id, with_accumulators=True)

    def reload_state(self, job_id: str) -> CrawlJobState | None:
        """The live state of a job, without its page history or visited set.
        The batch loop reloads once per batch; the accumulators come back empty
        because it is the one writer."""
        return self._read_row(job_id, with_accumulators=False)

    def _read_row(
        self, job_id: str, *, with_accumulators: bool, owner: str | None = None
    ) -> CrawlJobState | None:
        """The one row read both loaders share.
        Row select, collection reads and the walk hold one lock (reentrant), so
        the returned state is one generation on every route."""
        statement, params = _owner_statement(_ROW_COLUMNS, True, "", owner)
        try:
            # One hold AND one read snapshot across select, reads and walk; only
            # the deferred read transaction pins the WAL snapshot.
            with self._lock:
                self._conn.execute("BEGIN")
                try:
                    row = _fetch_owner_row(self._conn, statement, (job_id, *params))
                    if row is None:
                        self._conn.execute("COMMIT")
                        return None
                    factory = self._row_to_job_factory
                    if factory is None:
                        state = self._read_collections(row, with_accumulators=with_accumulators)
                    else:
                        state = factory(row, self._row_to_job, with_accumulators=with_accumulators)
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
                return state
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            # Surface corruption instead of silently returning None so a
            # broken row is observable in logs rather than masked.
            logger.warning("job_store_load_corrupt", extra={"job_id": job_id, "error": str(exc)})
            return None

    def load_for_caller(self, job_id: str, caller_id: str) -> CrawlJobState | None:
        """The job iff ``caller_id`` owns it; foreign/missing read the same miss.
        The owner is filtered in SQL before deserialization, so a foreign job_id is
        a cheap miss, not a full load then an owner compare."""
        return self._read_row(job_id, with_accumulators=True, owner=caller_id)

    def delete(self, job_id: str) -> bool:
        """Tombstone (hide) a job so DELETE is final: a late runner ``save`` cannot
        resurrect it. Returns True iff a live row matched. The page history and
        visited set go with the tombstone -- child tables of the deleted row.
        """
        return self._tombstone_row(
            job_id,
            "UPDATE crawl_jobs SET deleted_at = ?, updated_at = ? WHERE job_id = ? AND deleted_at IS NULL",
            (job_id,),
        )

    def delete_for_caller(self, job_id: str, caller_id: str) -> bool:
        """Tombstone iff ``caller_id`` owns a live row; a foreign id marks nothing."""
        return self._tombstone_row(
            job_id,
            "UPDATE crawl_jobs SET deleted_at = ?, updated_at = ? "
            "WHERE job_id = ? AND owner = ? AND deleted_at IS NULL",
            (job_id, caller_id),
        )

    def _tombstone_row(self, job_id: str, statement: str, params: tuple[object, ...]) -> bool:
        """Tombstone iff the ownership UPDATE matches; history dies with it."""
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
        """Whether a row exists marked deleted. Caller must hold ``self._lock``."""
        row = self._conn.execute(
            "SELECT 1 FROM crawl_jobs WHERE job_id = ? AND deleted_at IS NOT NULL", (job_id,)
        ).fetchone()
        return row is not None

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[JobStatusView]:
        """Every job's status view, newest first; an optional pagination slice."""
        return self._read_job_list(None, limit=limit, offset=offset)

    def list_for_caller(
        self, caller_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[JobStatusView]:
        """The caller's own jobs, newest first; foreign rows are not listed. An
        optional ``limit``/``offset`` slices the page at the SQL level so a
        caller with many rows never re-materializes the whole set."""
        return self._read_job_list(caller_id, limit=limit, offset=offset)

    def _read_job_list(self, owner: str | None, *, limit: int | None, offset: int = 0) -> list[JobStatusView]:
        """The status views owned by ``owner``, or every row when None, sliced
        by optional ``limit``/``offset`` (LIMIT is appended iff ``limit`` given)."""
        with self._lock:
            counts = self._accumulators.counts_by_job()
            statement, params = _owner_statement(_JOB_STATUS_PROJECTION, False, _JOB_LIST_ORDER, owner)
            if limit is not None:
                statement += " LIMIT ? OFFSET ?"
                params = (*params, max(0, limit), max(0, offset))
            rows = self._conn.execute(statement, params).fetchall()
        return [_status_row(r, *counts.get(r["job_id"], (0, 0))) for r in rows]

    def count_jobs(self) -> int:
        """Total live job rows (mirrors the unfiltered ``list_jobs`` primary)."""
        return self._count_jobs(None)

    def count_for_caller(self, caller_id: str) -> int:
        """Total live job rows owned by ``caller_id`` (mirrors ``list_for_caller``)."""
        return self._count_jobs(caller_id)

    def _count_jobs(self, owner: str | None) -> int:
        """Live row count, filtered to ``owner`` when one is given."""
        with self._lock:
            statement, params = _owner_statement("SELECT count(*) FROM crawl_jobs", False, "", owner)
            row = self._conn.execute(statement, params).fetchone()
        return int(row[0])

    def status_row(self, job_id: str) -> JobStatusView | None:
        """The status view for one job, or None if there is no such job.
        Deliberately not ``load``: the response exposes counts, and loading the state
        to count it would deserialize every payload and visited URL to throw them away.
        """
        return self._read_status_row(job_id, None)

    def status_for_caller(self, job_id: str, caller_id: str) -> JobStatusView | None:
        """The status view iff ``caller_id`` owns the row, else None."""
        return self._read_status_row(job_id, caller_id)

    def _read_status_row(self, job_id: str, owner: str | None) -> JobStatusView | None:
        """One job's status view, or None when no row matches the owner filter."""
        with self._lock:
            statement, params = _owner_statement(_JOB_STATUS_PROJECTION, True, "", owner)
            row = _fetch_owner_row(self._conn, statement, (job_id, *params))
            if row is None:
                return None
            visited, results = self._accumulators.counts_for(job_id)
        return _status_row(row, visited, results)

    def exists(self, job_id: str) -> bool:
        """Whether a job row exists, without reading it.

        The control plane's existence check is a gate, not a question about
        the job: anything that reads the row to answer it does more than asked.
        """
        return self._has_job_row(job_id, None)

    def exists_for_caller(self, job_id: str, caller_id: str) -> bool:
        """Whether ``caller_id`` owns a row under ``job_id``, without reading it."""
        return self._has_job_row(job_id, caller_id)

    def active_count_for_caller(self, caller_id: str) -> int:
        """Owned jobs still holding a runner slot: pending or running, never terminal."""
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) FROM crawl_jobs WHERE owner = ? AND status IN (?, ?) AND deleted_at IS NULL",
                (caller_id, JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchone()
        return int(row[0])

    def _has_job_row(self, job_id: str, owner: str | None) -> bool:
        """Whether a row matches, filtered to ``owner`` when one is given."""
        with self._lock:
            statement, params = _owner_statement("SELECT 1 FROM crawl_jobs", True, "", owner)
            row = _fetch_owner_row(self._conn, statement, (job_id, *params))
        return row is not None

    def close(self) -> None:
        """Close the database connection (thread-safe)."""
        with self._lock:
            self._conn.close()

    # One async twin per operation: ``atwin`` runs the sync call on a worker
    # thread, so the loop never waits on the store lock.

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
