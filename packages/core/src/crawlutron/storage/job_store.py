"""Job store: the single connection and lock over crawl-job persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from crawlutron.admission.policy import PolicyLike, PolicyTier, read_policy
from crawlutron.service.effects import EffectContainer
from crawlutron.storage.coordination_store import atwin
from crawlutron.storage.ids import new_job_id
from crawlutron.storage.migrations import StoreSchema
from crawlutron.storage.safe_db import (
    in_txn,
    txn_read,
    txn_write,
)
from crawlutron.storage.sqlite_store import (
    bind_conn_lifecycle,
    connect_store,
    connection_data_version,
    process_lock,
    store_lock_key,
)
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

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
from .job_store_list import read_job_list
from .job_store_read import (
    JOB_STATUS_PROJECTION,
    JobStatusView,
    normalize_job_state,
    owner_statement,
    row_to_state,
    status_row,
)
from .job_store_state import (
    _UPSERT_JOB_ROW_SQL,
    CrawlJobState,
    _create_job,
    _job_row_params,
)

_DEFAULT_CONTAINER = EffectContainer()


_ROW_COLUMNS = (
    "SELECT job_id, kind, owner, root_url, policy, status, created_at, updated_at, error, "
    "stats, in_flight, transient_retries, claimed_by, claimed_at, generation FROM crawl_jobs"
)


def _fetch_owner_row(
    conn: sqlite3.Connection, statement: str, params: tuple[object, ...]
) -> sqlite3.Row | None:
    """One owner-filtered row fetch: execute and fetchone, None on miss.
    Reentrant txn_read: callers already in txn_read join the outer snapshot;
    callers with no door (``_has_job_row``) get one here.
    """
    with txn_read(conn, owner="job_store._fetch_owner_row"):
        return conn.execute(statement, params).fetchone()


logger = logging.getLogger(__name__)

#: The admitted batch seeded at a single-shot job's birth (None = single URL).
type _Batch = list[FrontierEntry] | None


# set_status SQL is built in job_store_lease for file-size budget.


def _read_job_row(
    store: JobStore, job_id: str, *, with_accumulators: bool, owner: str | None = None
) -> CrawlJobState | None:
    statement, params = owner_statement(_ROW_COLUMNS, True, "", owner)
    try:
        with store._lock:
            with txn_read(store._conn, owner="job_store.read_job_row"):
                row = _fetch_owner_row(store._conn, statement, (job_id, *params))
                if row is None:
                    return None
                factory = store._row_to_job_factory
                if factory is None:
                    state = store._read_collections(row, with_accumulators=with_accumulators)
                else:
                    state = factory(row, store._row_to_job, with_accumulators=with_accumulators)
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
    def __init__(
        self,
        db_path: str | Path,
        *,
        policy: HostPolicy,
        tier: PolicyTier,
        row_to_job: Callable[..., CrawlJobState] | None = None,
        container: EffectContainer | None = None,
    ):
        self.db_path = Path(db_path)
        self._host_policy = policy
        self.tier = tier
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn, memory_name = connect_store(self.db_path, schema=StoreSchema.CRAWL_JOBS)
        self.lock_key = store_lock_key(self.db_path, memory_name)
        self._lock = process_lock(self.lock_key)
        self._conn.row_factory = sqlite3.Row
        bind_conn_lifecycle(self)
        self._row_to_job_factory: Callable[..., CrawlJobState] | None = row_to_job
        self._container = container or _DEFAULT_CONTAINER
        self._accumulators = JobAccumulators(self._conn)

    def _read_collections(self, row: sqlite3.Row, *, with_accumulators: bool) -> CrawlJobState:
        """Collections read under the lock; both loaders share this atomic hold."""
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
        """One row read through the declared record; the frontier always loads."""
        return self._read_collections(row, with_accumulators=with_accumulators)

    def create(
        self, root_url: str, policy: PolicyLike, *, owner: str = "", job_id: str | None = None
    ) -> CrawlJobState:
        canonical_root = NormalizedUrl(root_url, policy=self._host_policy)
        return _create_job(
            self,
            job_id or new_job_id(),
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
        with self._lock:
            self._persist(job)

    def _persist(self, job: CrawlJobState) -> None:
        with txn_write(self._conn, owner="job_store.save"):
            if self._is_tombstoned(job.job_id):
                return
            self._accumulators._seed_table_truth(job.job_id)
            version = connection_data_version(self._conn)
            job.updated_at = time.time()
            applied = self._conn.execute(_UPSERT_JOB_ROW_SQL, _job_row_params(job)).rowcount == 1
            self._accumulators.append(job)
        self._accumulators._seed_version(job.job_id, version)
        if applied:
            job.generation += 1

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
            with txn_write(self._conn, owner="job_store.tombstone"):
                if self._is_tombstoned(job_id):
                    return False
                cur = self._conn.execute(statement, (time.time(), time.time(), *params))
                if cur.rowcount != 1:
                    return False
                self._accumulators.delete(job_id)
        return True

    def _is_tombstoned(self, job_id: str) -> bool:
        with self._lock:
            with in_txn(self._conn):
                row = self._conn.execute(
                    "SELECT 1 FROM crawl_jobs WHERE job_id = ? AND deleted_at IS NOT NULL", (job_id,)
                ).fetchone()
        return row is not None

    def reap_tombstones(self, *, grace_seconds: float, now: float) -> int:
        """Reclaim tombstones past grace, cascading their accumulators.

        Younger than grace: left untouched; idempotent deletes stay safe.
        """
        cutoff = now - max(0.0, float(grace_seconds))
        reclaimed = 0
        with self._lock:
            with txn_write(self._conn, owner="job_store.reap_tombstones"):
                rows = self._conn.execute(
                    "SELECT job_id FROM crawl_jobs WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    job_id = row["job_id"]
                    self._conn.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))
                    self._accumulators.delete(job_id)
                    reclaimed += 1
        return reclaimed

    def reap_orphan_accumulators(self) -> int:
        """Delete accumulator rows whose job_id is absent from crawl_jobs."""
        total = 0
        with self._lock:
            with txn_write(self._conn, owner="job_store.reap_orphan_accumulators"):
                for sql in (
                    'DELETE FROM "crawl_job_results" WHERE job_id NOT IN (SELECT job_id FROM crawl_jobs)',
                    'DELETE FROM "crawl_job_visited" WHERE job_id NOT IN (SELECT job_id FROM crawl_jobs)',
                    'DELETE FROM "crawl_job_frontier" WHERE job_id NOT IN (SELECT job_id FROM crawl_jobs)',
                ):
                    cur = self._conn.execute(sql)
                    total += cur.rowcount
        return total

    def _list_jobs(
        self,
        owner: str | None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobStatusView]:
        with self._lock:
            return read_job_list(self._conn, owner, limit=limit, offset=offset)

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[JobStatusView]:
        return self._list_jobs(None, limit=limit, offset=offset)

    def list_for_caller(
        self, caller_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[JobStatusView]:
        return self._list_jobs(caller_id, limit=limit, offset=offset)

    def count_jobs(self) -> int:
        return self._count_jobs(None)

    def count_for_caller(self, caller_id: str) -> int:
        return self._count_jobs(caller_id)

    def _count_jobs(self, owner: str | None) -> int:
        with self._lock:
            statement, params = owner_statement("SELECT count(*) FROM crawl_jobs", False, "", owner)
            with txn_read(self._conn, owner="job_store._count_jobs"):
                row = self._conn.execute(statement, params).fetchone()
        return int(row[0])

    def status_row(self, job_id: str) -> JobStatusView | None:
        return self._read_status_row(job_id, None)

    def status_for_caller(self, job_id: str, caller_id: str) -> JobStatusView | None:
        return self._read_status_row(job_id, caller_id)

    def _read_status_row(self, job_id: str, owner: str | None) -> JobStatusView | None:
        with self._lock:
            statement, params = owner_statement(JOB_STATUS_PROJECTION, True, "", owner)
            row = _fetch_owner_row(self._conn, statement, (job_id, *params))
            if row is None:
                return None
            visited, results = self._accumulators.counts_for(job_id)
        return status_row(row, visited, results)

    def exists(self, job_id: str) -> bool:
        return self._has_job_row(job_id, None)

    def exists_for_caller(self, job_id: str, caller_id: str) -> bool:
        return self._has_job_row(job_id, caller_id)

    def active_count_for_caller(self, caller_id: str) -> int:
        with self._lock:
            with txn_read(self._conn, owner="job_store.active_count_for_caller"):
                row = self._conn.execute(
                    "SELECT count(*) FROM crawl_jobs WHERE owner = ?"
                    " AND status IN (?, ?) AND deleted_at IS NULL",
                    (caller_id, JobStatus.PENDING.value, JobStatus.RUNNING.value),
                ).fetchone()
        return int(row[0])

    def _has_job_row(self, job_id: str, owner: str | None) -> bool:
        with self._lock:
            statement, params = owner_statement("SELECT 1 FROM crawl_jobs", True, "", owner)
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

    async def asave(self, job: CrawlJobState) -> None:
        await self._container.db_write(lambda: self.save(job), owner="job_store.save")

    async def adelete(self, job_id: str) -> bool:
        return await self._container.db_write(lambda: self.delete(job_id), owner="job_store.delete")

    acreate = atwin(create)
    acreate_single_shot = atwin(create_single_shot)
    aset_status = atwin(set_status)
    aload = atwin(load)
    aload_for_caller = atwin(load_for_caller)
    areload_state = atwin(reload_state)
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
