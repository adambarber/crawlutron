"""Page-first job listing: LIMIT binds at the crawl_jobs scan, counts never run."""

from __future__ import annotations

import sqlite3

from crawlutron.storage.safe_db import txn_read

from .job_store_read import (
    JOB_LIST_ORDER,
    JOB_STATUS_PROJECTION,
    owner_statement,
    status_row,
)


def read_job_list(
    conn: sqlite3.Connection,
    owner: str | None,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list:
    """One page of job status views: page IDs first, full rows second.

    Reentrant: joins the caller's txn_read/txn_write if present; otherwise
    starts its own read transaction.
    """
    statement, params = owner_statement("SELECT job_id FROM crawl_jobs", False, JOB_LIST_ORDER, owner)
    if limit is not None:
        statement += " LIMIT ? OFFSET ?"
        params = (*params, max(0, limit), max(0, offset))
    with txn_read(conn, owner="job_store.read_job_list"):
        page_ids = [row["job_id"] for row in conn.execute(statement, params).fetchall()]
    if not page_ids:
        return []
    placeholders = ", ".join("?" for _ in page_ids)
    statement = JOB_STATUS_PROJECTION + " WHERE job_id IN (" + placeholders + ") " + JOB_LIST_ORDER
    with txn_read(conn, owner="job_store.read_job_list"):
        rows = conn.execute(statement, (*page_ids,)).fetchall()
    return [status_row(r, 0, 0) for r in rows]
