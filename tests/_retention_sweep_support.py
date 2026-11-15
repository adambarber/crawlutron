"""Shared helpers for retention sweep tests: store creation and job tombstoning."""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import closing

from crawlutron.storage.job_store_collections import OrderedUrlSet
from crawlutron.storage.result_store import CrawlResultStore
from crawlutron.storage.safe_db import allow_on_loop
from tests._policy_factories import canonical
from tests._policy_factories import job_store as make_job_store

GRACE = 86400.0
TTL = 3600.0
NOW = 1000.0


def stores(tmp_path):
    """One JobStore and one CrawlResultStore sharing the test database."""
    db = pathlib.Path(tmp_path) / "crawler.db"
    with allow_on_loop():
        job_store = make_job_store(db)
        result_store = CrawlResultStore(db, ttl_seconds=TTL, clock=lambda: NOW)
    return job_store, result_store


def tombstone_job(job_store, job_id, deleted_at=NOW):
    """Tombstone a job and backdate its deleted_at to a fixed instant."""
    job_store.delete(job_id)
    with closing(sqlite3.connect(job_store.db_path)) as conn:
        conn.execute(
            "UPDATE crawl_jobs SET deleted_at = ? WHERE job_id = ?",
            (deleted_at, job_id),
        )
        conn.commit()


def root_job(job_store):
    """Create a job with a visited URL and a result, then tombstone it."""
    job = job_store.create("http://example.com/", {"strategies": ["HTTP_DECLARED_CRAWLER"]})
    job.visited = OrderedUrlSet([canonical("http://example.com/a")])
    job.results = [{"url": "http://example.com/a", "status": "success"}]
    job_store.save(job)
    tombstone_job(job_store, job.job_id)
    return job


def tombstoned_job_with_expired_result(job_store, result_store):
    """Create a tombstoned job and put an expired result row."""
    job = root_job(job_store)
    with allow_on_loop():
        result_store.put("req-expired", {"status": "ok"})
    return job


def tombstoned_stores_with_expired_result(tmp_path):
    """Create stores, tombstone a job, put an expired result; return (job_store, result_store, job)."""
    job_store, result_store = stores(tmp_path)
    job = tombstoned_job_with_expired_result(job_store, result_store)
    return job_store, result_store, job


def run_retention_maintenance_once(job_store, result_store):
    """Run retention_maintenance_once with default grace and a NOW + grace + 1s clock."""
    from crawlutron.service.retention_sweep import retention_maintenance_once

    return retention_maintenance_once(job_store, result_store, grace_seconds=GRACE, now=NOW + GRACE + 1.0)


def run_sweep_on_tombstoned_stores(tmp_path):
    """Create stores, tombstone a job, put an expired result, run the sweep.

    Returns (job_store, result_store, job, reclaimed).
    """
    job_store, result_store, job = tombstoned_stores_with_expired_result(tmp_path)
    reclaimed = run_retention_maintenance_once(job_store, result_store)
    return job_store, result_store, job, reclaimed


def assert_sweep_reclaimed_job_and_result(reclaimed, job_store, result_store, job):
    """Assert the sweep reclaimed at least 2 rows and both the job and result are gone."""
    assert reclaimed >= 2, "tombstone plus expired result must both be reclaimed"
    assert job_store.load(job.job_id) is None
    assert result_store.get("req-expired") is None


def sweep_reclaims_tombstone_and_expired_result(tmp_path):
    """Shared test body: retention_maintenance_once reclaims a tombstoned job and an expired result."""
    job_store, result_store, job, reclaimed = run_sweep_on_tombstoned_stores(tmp_path)
    assert_sweep_reclaimed_job_and_result(reclaimed, job_store, result_store, job)
    job_store.close()
    result_store.close()


async def assert_lifespan_sweep_task(app, task_attr: str, task_name: str) -> None:
    """Assert the lifespan starts and drains a sweep task."""
    async with app.router.lifespan_context(app):
        task = getattr(app.state, task_attr)
        assert task is not None, f"startup must start the {task_name} task"
        assert not task.done()
    assert task.done(), f"shutdown must drain the {task_name} task"
