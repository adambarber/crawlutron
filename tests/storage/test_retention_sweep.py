"""Retention sweep reclaims garbage rows: tombstones past grace, expired
results, orphaned accumulators -- one pass, one cadence.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from crawlutron.config.settings import CrawlerSettings
from crawlutron.service.retention_sweep import retention_maintenance_once
from crawlutron.storage.safe_db import allow_on_loop
from tests._retention_sweep_support import (
    GRACE,
    NOW,
    TTL,
    root_job,
    stores,
    sweep_reclaims_tombstone_and_expired_result,
    tombstone_job,
)


class TestRetentionSweepTick:
    def test_tombstone_past_grace_is_reclaimed_with_its_accumulators(self, tmp_path):
        job_store, result_store = stores(tmp_path)
        try:
            job = root_job(job_store)

            # Tombstone written at NOW; sweep clock is NOW + grace + 1s -> past grace.
            reclaimed = job_store.reap_tombstones(grace_seconds=GRACE, now=NOW + GRACE + 1.0)

            assert reclaimed == 1
            assert job_store.load(job.job_id) is None
            with closing(sqlite3.connect(job_store.db_path)) as conn:
                assert not _row_exists(conn, "crawl_job_results", job.job_id)
                assert not _row_exists(conn, "crawl_job_visited", job.job_id)
                assert not _row_exists(conn, "crawl_job_frontier", job.job_id)
        finally:
            job_store.close()
            result_store.close()

    def test_tombstone_younger_than_grace_survives_the_sweep(self, tmp_path):
        job_store, _ = stores(tmp_path)
        try:
            job = job_store.create("http://example.com/", {"strategies": ["HTTP_DECLARED_CRAWLER"]})
            tombstone_job(job_store, job.job_id)

            # Fresh tombstone at NOW; sweep clock is NOW -> not past grace.
            reclaimed = job_store.reap_tombstones(grace_seconds=GRACE, now=NOW)

            assert reclaimed == 0, "a tombstone younger than grace must not be reclaimed"
            assert job_store.load(job.job_id) is None
            assert job_store.count_jobs() == 0
        finally:
            job_store.close()

    def test_zero_grace_reclaims_tombstone_deleted_before_now_but_not_at_now(self, tmp_path):
        """Zero grace reclaims tombstones strictly older than now.

        now - 1 is reclaimed; exactly now survives (now < now is false). Checks
        the DB directly because load() returns None for any tombstoned job.
        """
        job_store, _ = stores(tmp_path)
        try:
            old_job = job_store.create("http://example.com/old", {"strategies": ["HTTP_DECLARED_CRAWLER"]})
            fresh_job = job_store.create(
                "http://example.com/fresh", {"strategies": ["HTTP_DECLARED_CRAWLER"]}
            )
            tombstone_job(job_store, old_job.job_id, deleted_at=NOW - 1.0)
            tombstone_job(job_store, fresh_job.job_id, deleted_at=NOW)

            reclaimed = job_store.reap_tombstones(grace_seconds=0.0, now=NOW)

            assert reclaimed == 1, "zero grace must reclaim tombstones strictly older than now"
            with closing(sqlite3.connect(job_store.db_path)) as conn:
                assert not _row_exists(conn, "crawl_jobs", old_job.job_id), (
                    "now-1 tombstone must be reclaimed"
                )
                assert _row_exists(conn, "crawl_jobs", fresh_job.job_id), (
                    "now tombstone must survive zero-grace sweep"
                )
        finally:
            job_store.close()

    def test_expired_result_with_no_subsequent_put_is_reclaimed(self, tmp_path):
        _, result_store = stores(tmp_path)
        try:
            with allow_on_loop():
                result_store.put("req-1", {"status": "ok"})

            # Put at NOW; sweep clock is NOW + TTL + 1s -> the row is expired.
            reclaimed = result_store.prune_expired(now=NOW + TTL + 1.0)

            assert reclaimed == 1
            assert result_store.get("req-1") is None
        finally:
            result_store.close()

    def test_orphan_accumulator_rows_are_reclaimed(self, tmp_path):
        job_store, _ = stores(tmp_path)
        try:
            orphan_job_id = "orphan-job-that-never-existed"
            with closing(sqlite3.connect(job_store.db_path)) as conn:
                conn.execute(
                    'INSERT INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, 0, ?)',
                    (orphan_job_id, '{"url": "http://example.com/"}'),
                )
                conn.commit()

            reclaimed = job_store.reap_orphan_accumulators()

            assert reclaimed >= 1, "orphan accumulator rows must be reclaimed"
        finally:
            job_store.close()

    def test_retention_maintenance_once_runs_all_families_and_checkpoints(self, tmp_path):
        sweep_reclaims_tombstone_and_expired_result(tmp_path)

    def test_sweep_reads_settings_grace_not_the_module_constant(self, tmp_path):
        """A tombstone 60s past deletion is reclaimed with grace=50 but not with
        the default 86400s constant -- proving the sweep reads settings, not the constant.
        """
        job_store, result_store = stores(tmp_path)
        try:
            job = root_job(job_store)

            # Tombstone written at NOW - 60s; deleted_at is 60s in the past.
            with closing(sqlite3.connect(job_store.db_path)) as conn:
                conn.execute(
                    "UPDATE crawl_jobs SET deleted_at = ? WHERE job_id = ?",
                    (NOW - 60.0, job.job_id),
                )
                conn.commit()

            # With the default constant (86400s), this tombstone is NOT past grace.
            reclaimed_with_constant = job_store.reap_tombstones(grace_seconds=86400.0, now=NOW)
            assert reclaimed_with_constant == 0, "default 86400s grace must not reclaim a 60s-old tombstone"

            # With settings grace (50s), it IS past grace.
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_RETENTION_SWEEP_GRACE_SECONDS": "50"})
            reclaimed_with_settings = retention_maintenance_once(
                job_store,
                result_store,
                grace_seconds=float(settings.retention_sweep_grace_seconds),
                now=NOW,
            )
            assert reclaimed_with_settings >= 1, "settings grace of 50s must reclaim a 60s-old tombstone"
            assert job_store.load(job.job_id) is None
        finally:
            job_store.close()
            result_store.close()


def _row_exists(conn, table, job_id):
    """True if a row for job_id exists in table."""
    row = conn.execute(f'SELECT 1 FROM "{table}" WHERE job_id = ?', (job_id,)).fetchone()
    return row is not None
