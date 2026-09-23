"""Migration-ledger adoption, rollback, and live-schema conformance."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.crawl.job_store import JobStatus, JobStore
from app.crawl.migrations import (
    CURRENT_SCHEMAS,
    MIGRATION_IDS,
    StoreSchema,
    create_table_sql,
    rollback_store,
    schema_drift,
    stamp_store,
)
from app.crawl.result_store import CrawlResultStore
from app.validation import HostPolicy

_BOOKKEEPING_TABLES = {"_yoyo_log", "_yoyo_migration", "_yoyo_version", "yoyo_lock"}


def _open_store(schema: StoreSchema, db_path: Path) -> JobStore | CrawlResultStore:
    if schema is StoreSchema.CRAWL_JOBS:
        return JobStore(db_path, policy=HostPolicy.unrestricted())
    return CrawlResultStore(db_path)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _assert_migration_recorded(conn: sqlite3.Connection, schema: StoreSchema) -> None:
    rows = conn.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
    assert MIGRATION_IDS[schema] in {str(row[0]) for row in rows}


def _create_legacy_database(db_path: Path, schema: StoreSchema) -> None:
    table = CURRENT_SCHEMAS[schema]
    conn = sqlite3.connect(db_path)
    conn.execute(create_table_sql(table))
    if schema is StoreSchema.CRAWL_JOBS:
        conn.execute(
            """
            INSERT INTO crawl_jobs (
                job_id, kind, root_url, policy, status, frontier, visited,
                in_flight, transient_retries, results, created_at, updated_at,
                error, stats
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "whole_site",
                "https://legacy.example",
                '{"limits": {"max_depth": 2}}',
                "paused",
                '[]',
                '["https://legacy.example"]',
                '[]',
                '{"https://legacy.example": 1}',
                '[]',
                10.0,
                20.0,
                None,
                '{"total_discovered": 1}',
            ),
        )
    else:
        conn.execute(
            "INSERT INTO results (request_id, stored_at, payload) VALUES (?, ?, ?)",
            ("legacy-request", 30.0, '{"request_id": "legacy-request", "results": []}'),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("schema", list(StoreSchema))
def test_fresh_database_is_empty_and_current_after_migrations(tmp_path, schema):
    db_path = tmp_path / f"{schema.value}.db"
    store = _open_store(schema, db_path)
    store.close()

    conn = sqlite3.connect(db_path)
    assert CURRENT_SCHEMAS[schema].table_name in _table_names(conn)
    assert schema_drift(conn, CURRENT_SCHEMAS[schema]) == ()
    assert conn.execute(f"SELECT COUNT(*) FROM {CURRENT_SCHEMAS[schema].table_name}").fetchone()[0] == 0
    assert _BOOKKEEPING_TABLES <= _table_names(conn)
    _assert_migration_recorded(conn, schema)
    conn.close()


def test_legacy_job_database_is_stamped_without_losing_rows_or_columns(tmp_path):
    db_path = tmp_path / "legacy-jobs.db"
    _create_legacy_database(db_path, StoreSchema.CRAWL_JOBS)
    before = sqlite3.connect(db_path)
    assert "_yoyo_migration" not in _table_names(before)
    before.close()

    store = JobStore(db_path, policy=HostPolicy.unrestricted())
    loaded = store.load("legacy-job")

    assert loaded is not None
    assert loaded.root_url == "https://legacy.example"
    assert loaded.status is JobStatus.PAUSED
    assert loaded.visited == {"https://legacy.example"}
    assert loaded.transient_retries == {"https://legacy.example": 1}
    conn = sqlite3.connect(db_path)
    assert schema_drift(conn, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]) == ()
    assert tuple(row[1] for row in conn.execute("PRAGMA table_info(crawl_jobs)")) == tuple(
        column.name for column in CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS].columns
    )
    _assert_migration_recorded(conn, StoreSchema.CRAWL_JOBS)
    conn.close()
    store.close()


def test_historical_canonicalized_column_is_removed_during_legacy_stamping(tmp_path):
    db_path = tmp_path / "historical-jobs.db"
    _create_legacy_database(db_path, StoreSchema.CRAWL_JOBS)
    before = sqlite3.connect(db_path)
    before.execute(
        "ALTER TABLE crawl_jobs ADD COLUMN canonicalized INTEGER NOT NULL DEFAULT 0"
    )
    before.commit()
    before.close()

    store = JobStore(db_path, policy=HostPolicy.unrestricted())
    loaded = store.load("legacy-job")
    after = sqlite3.connect(db_path)
    columns = {str(row[1]) for row in after.execute("PRAGMA table_info(crawl_jobs)")}

    assert loaded is not None
    assert "canonicalized" not in columns
    assert schema_drift(after, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]) == ()
    _assert_migration_recorded(after, StoreSchema.CRAWL_JOBS)
    after.close()
    store.close()


def test_explicit_legacy_stamp_allows_historical_column_but_drift_remains_strict(tmp_path):
    db_path = tmp_path / "stamped-historical-jobs.db"
    _create_legacy_database(db_path, StoreSchema.CRAWL_JOBS)
    before = sqlite3.connect(db_path)
    before.execute("ALTER TABLE crawl_jobs ADD COLUMN canonicalized INTEGER NOT NULL DEFAULT 0")
    before.commit()
    before.close()

    stamp_store(db_path, StoreSchema.CRAWL_JOBS)

    after = sqlite3.connect(db_path)
    columns = {str(row[1]) for row in after.execute("PRAGMA table_info(crawl_jobs)")}
    assert "canonicalized" in columns
    assert schema_drift(after, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS])
    _assert_migration_recorded(after, StoreSchema.CRAWL_JOBS)
    after.close()


def test_legacy_result_database_is_stamped_without_losing_rows_or_columns(tmp_path):
    db_path = tmp_path / "legacy-results.db"
    _create_legacy_database(db_path, StoreSchema.RESULTS)
    before = sqlite3.connect(db_path)
    assert "_yoyo_migration" not in _table_names(before)
    before.close()

    store = CrawlResultStore(db_path, ttl_seconds=0)

    assert store.get("legacy-request") == {"request_id": "legacy-request", "results": []}
    conn = sqlite3.connect(db_path)
    assert schema_drift(conn, CURRENT_SCHEMAS[StoreSchema.RESULTS]) == ()
    assert tuple(row[1] for row in conn.execute("PRAGMA table_info(results)")) == tuple(
        column.name for column in CURRENT_SCHEMAS[StoreSchema.RESULTS].columns
    )
    _assert_migration_recorded(conn, StoreSchema.RESULTS)
    conn.close()
    store.close()


def test_missing_migration_column_is_detected_as_drift(tmp_path):
    db_path = tmp_path / "missing-column.db"
    store = JobStore(db_path, policy=HostPolicy.unrestricted())
    store.close()
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE crawl_jobs DROP COLUMN transient_retries")
    conn.commit()
    conn.close()

    drifted = sqlite3.connect(db_path)
    assert schema_drift(drifted, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS])
    drifted.close()


def test_out_of_band_extra_column_is_detected_as_drift(tmp_path):
    db_path = tmp_path / "extra-column.db"
    store = JobStore(db_path, policy=HostPolicy.unrestricted())
    store.close()
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN unexpected TEXT")
    conn.commit()
    conn.close()

    drifted = sqlite3.connect(db_path)
    assert schema_drift(drifted, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS])
    drifted.close()


def test_out_of_band_changed_default_is_detected_as_drift(tmp_path):
    db_path = tmp_path / "changed-default.db"
    store = JobStore(db_path, policy=HostPolicy.unrestricted())
    store.close()
    conn = sqlite3.connect(db_path)
    changed_schema = create_table_sql(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]).replace(
        "DEFAULT 'whole_site'", "DEFAULT 'single_shot'"
    )
    conn.execute("ALTER TABLE crawl_jobs RENAME TO crawl_jobs_old")
    conn.execute(changed_schema)
    conn.execute("DROP TABLE crawl_jobs_old")
    conn.commit()
    conn.close()

    drifted = sqlite3.connect(db_path)
    assert schema_drift(drifted, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS])
    drifted.close()


def test_migration_bookkeeping_never_leaks_into_store_payloads(tmp_path):
    job_store = JobStore(tmp_path / "jobs.db", policy=HostPolicy.unrestricted())
    result_store = CrawlResultStore(tmp_path / "results.db")
    job = job_store.create("https://example.test", {})
    job.results = [{"url": "https://example.test", "status": "success"}]
    job_store.save(job)
    result_store.put("request-1", {"request_id": "request-1", "results": []})

    jobs = job_store.list_jobs()
    result = result_store.get("request-1")
    wire_payload = json.dumps({"jobs": jobs, "result": result}, sort_keys=True)

    assert jobs[0]["results_count"] == 1
    for table_name in _BOOKKEEPING_TABLES:
        assert table_name not in wire_payload
    job_store.close()
    result_store.close()


@pytest.mark.parametrize("schema", list(StoreSchema))
def test_migration_rollback_is_reversible_and_updates_the_ledger(tmp_path, schema):
    db_path = tmp_path / f"rollback-{schema.value}.db"
    store = _open_store(schema, db_path)
    store.close()
    rollback_store(db_path, schema)

    conn = sqlite3.connect(db_path)
    assert CURRENT_SCHEMAS[schema].table_name not in _table_names(conn)
    applied = {
        str(row[0]) for row in conn.execute("SELECT migration_id FROM _yoyo_migration")
    }
    assert MIGRATION_IDS[schema] not in applied
    conn.close()
