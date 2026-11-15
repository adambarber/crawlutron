"""Runner-lease SQL operations for ``crawl_jobs``.

Extracted from ``JobStore`` to keep the store class and file within budgets.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from crawlutron.storage.job_store_collections import JobStatus
from crawlutron.storage.safe_db import txn_write

DEFAULT_JOB_CLAIM_SECONDS = 300.0


def _execute(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    statement: str,
    params: tuple[object, ...],
) -> sqlite3.Cursor:
    with lock:
        with txn_write(conn, owner="job_store_lease"):
            cur = conn.execute(statement, params)
    return cur


def claim_running(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
    token: str,
    *,
    now: float,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> bool:
    """Claim a RUNNING row for ``token`` when unclaimed/stale/self-owned."""
    stale_before = now - claim_seconds
    statement = """
        UPDATE crawl_jobs
           SET claimed_by = ?, claimed_at = ?, updated_at = ?
         WHERE job_id = ? AND deleted_at IS NULL AND status = ?
           AND (claimed_by IS NULL OR claimed_at IS NULL
                OR claimed_at < ? OR claimed_by = ?)
    """
    params = (token, now, now, job_id, JobStatus.RUNNING.value, stale_before, token)
    return _execute(conn, lock, statement, params).rowcount == 1


def renew_job_claim(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
    token: str,
    *,
    now: float,
) -> bool:
    """Refresh ``claimed_at`` while ``token`` still owns the RUNNING row."""
    statement = """
        UPDATE crawl_jobs
           SET claimed_at = ?, updated_at = ?
         WHERE job_id = ? AND deleted_at IS NULL AND status = ?
           AND claimed_by = ?
    """
    params = (now, now, job_id, JobStatus.RUNNING.value, token)
    return _execute(conn, lock, statement, params).rowcount == 1


def release_job_claim(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
    token: str,
) -> bool:
    """Release the lease when ``token`` still owns it."""
    statement = """
        UPDATE crawl_jobs
           SET claimed_by = NULL, claimed_at = NULL, updated_at = ?
         WHERE job_id = ? AND deleted_at IS NULL AND claimed_by = ?
    """
    params = (time.time(), job_id, token)
    return _execute(conn, lock, statement, params).rowcount == 1


def clear_job_claim(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
) -> bool:
    """Drop any lease on the row regardless of owner."""
    statement = """
        UPDATE crawl_jobs
           SET claimed_by = NULL, claimed_at = NULL, updated_at = ?
         WHERE job_id = ? AND deleted_at IS NULL
    """
    params = (time.time(), job_id)
    return _execute(conn, lock, statement, params).rowcount == 1


def running_claim(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
) -> tuple[str | None, float | None, str | None] | None:
    """Return ``(claimed_by, claimed_at, status)`` for the row."""
    statement = "SELECT claimed_by, claimed_at, status FROM crawl_jobs WHERE job_id = ?"
    with lock:
        row = conn.execute(statement, (job_id,)).fetchone()
    if row is None:
        return None
    claimed_by = row[0] if isinstance(row[0], str) and row[0] else None
    claimed_at = float(row[1]) if isinstance(row[1], (int, float)) and not isinstance(row[1], bool) else None
    status = row[2] if isinstance(row[2], str) else None
    return claimed_by, claimed_at, status


def is_job_claim_fresh(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
    *,
    now: float,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> bool:
    """True when the row is RUNNING with a non-stale lease."""
    claim = running_claim(conn, lock, job_id)
    if claim is None:
        return False
    claimed_by, claimed_at, status = claim
    if status != JobStatus.RUNNING.value or claimed_by is None or claimed_at is None:
        return False
    return claimed_at >= now - claim_seconds


def retire_orphan_running(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
    new: JobStatus,
    *,
    now: float,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
    error: str | None = None,
) -> bool:
    """CAS RUNNING→terminal only when unclaimed or stale at ``now``."""
    stale_before = now - claim_seconds
    statement = """
        UPDATE crawl_jobs
           SET status = ?, updated_at = ?, error = COALESCE(?, error),
               claimed_by = NULL, claimed_at = NULL
         WHERE job_id = ? AND deleted_at IS NULL AND status = ?
           AND (claimed_by IS NULL OR claimed_at IS NULL OR claimed_at < ?)
    """
    params = (new.value, now, error, job_id, JobStatus.RUNNING.value, stale_before)
    return _execute(conn, lock, statement, params).rowcount == 1


def _status_with_claim(
    wanted: set[JobStatus],
    new: JobStatus,
    *,
    wanted_json: str,
    now: float,
    error: str | None,
    job_id: str,
    claimed_by: str,
    claim_now: float,
    claim_seconds: float,
) -> tuple[str, tuple[object, ...]]:
    if wanted == {JobStatus.RUNNING} and new is JobStatus.RUNNING:
        stmt = (
            "UPDATE crawl_jobs SET status=?, updated_at=?, error=COALESCE(?, error), "
            "claimed_by=?, claimed_at=? WHERE job_id=? AND deleted_at IS NULL "
            "AND status IN (SELECT value FROM json_each(?)) "
            "AND (claimed_by IS NULL OR claimed_at IS NULL OR claimed_at < ? OR claimed_by = ?)"
        )
        params = (
            new.value,
            now,
            error,
            claimed_by,
            claim_now,
            job_id,
            wanted_json,
            claim_now - claim_seconds,
            claimed_by,
        )
        return stmt, params
    stmt = (
        "UPDATE crawl_jobs SET status=?, updated_at=?, error=COALESCE(?, error), "
        "claimed_by=?, claimed_at=? WHERE job_id=? AND deleted_at IS NULL "
        "AND status IN (SELECT value FROM json_each(?))"
    )
    return stmt, (new.value, now, error, claimed_by, claim_now, job_id, wanted_json)


def set_status(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    job_id: str,
    new: JobStatus,
    *,
    expected: JobStatus | set[JobStatus] | frozenset[JobStatus],
    error: str | None = None,
    claimed_by: str | None = None,
    claim_now: float | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
    require_owner: bool = False,
    clear_claim: bool = False,
) -> bool:
    if clear_claim and claim_now is not None:
        raise ValueError("set_status: clear_claim conflicts with claim_now")
    wanted = {expected} if isinstance(expected, JobStatus) else set(expected)
    now = claim_now if claim_now is not None else time.time()
    wanted_json = json.dumps(sorted(s.value for s in wanted))
    if claimed_by is not None and claim_now is not None:
        stmt, params = _status_with_claim(
            wanted,
            new,
            wanted_json=wanted_json,
            now=now,
            error=error,
            job_id=job_id,
            claimed_by=claimed_by,
            claim_now=claim_now,
            claim_seconds=claim_seconds,
        )
    elif clear_claim:
        stmt = (
            "UPDATE crawl_jobs SET status=?, updated_at=?, error=COALESCE(?, error), "
            "claimed_by=NULL, claimed_at=NULL WHERE job_id=? AND deleted_at IS NULL "
            "AND status IN (SELECT value FROM json_each(?))"
        )
        params = (new.value, now, error, job_id, wanted_json)
    else:
        stmt = (
            "UPDATE crawl_jobs SET status=?, updated_at=?, error=COALESCE(?, error) "
            "WHERE job_id=? AND deleted_at IS NULL AND status IN (SELECT value FROM json_each(?))"
        )
        params = (new.value, now, error, job_id, wanted_json)
    if (
        require_owner
        and claimed_by is not None
        and (clear_claim or not (claimed_by is not None and claim_now is not None))
    ):
        stmt += " AND claimed_by=?"
        params = (*params, claimed_by)
    return _execute(conn, lock, stmt, params).rowcount == 1
