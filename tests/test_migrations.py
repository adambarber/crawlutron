"""Migration-ledger adoption, rollback, and live-schema conformance."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.caller_key_store import CallerKeyStore
from crawlutron.storage.coordination_cookies import SharedCookieStore
from crawlutron.storage.coordination_counters import CoordinationCounters
from crawlutron.storage.job_store import CrawlJobState, JobStatus, JobStore
from crawlutron.storage.migrations import (
    CURRENT_SCHEMAS,
    LEGACY_COLUMNS,
    MIGRATION_IDS,
    ColumnSchema,
    StoreSchema,
    TableSchema,
    create_table_sql,
    rollback_store,
    schema_drift,
    stamp_store,
)
from crawlutron.storage.migrations.runner import LegacySchemaMismatch, migrate_store
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from crawlutron.storage.watch_store import WatchStore
from tests._policy_factories import canonical, job_store

_BOOKKEEPING_TABLES = {"_yoyo_log", "_yoyo_migration", "_yoyo_version", "yoyo_lock"}


def _open_store(
    schema: StoreSchema, db_path: Path
) -> (
    JobStore
    | CrawlResultStore
    | CoordinationCounters
    | SharedCookieStore
    | WatchStore
    | CallerKeyStore
    | CrawlSpillStore
):
    if schema is StoreSchema.CRAWL_JOBS:
        return job_store(db_path)
    if schema is StoreSchema.RESULTS:
        return CrawlResultStore(db_path)
    if schema is StoreSchema.COORDINATION_COUNTERS:
        return CoordinationCounters(db_path)
    if schema is StoreSchema.COORDINATION_COOKIES:
        return SharedCookieStore(db_path)
    if schema is StoreSchema.WATCHES:
        return WatchStore(db_path)
    if schema is StoreSchema.CALLER_KEYS:
        return CallerKeyStore(db_path)
    if schema is StoreSchema.CRAWL_SPILLS:
        return CrawlSpillStore(db_path)
    # The parametrization above is derived from StoreSchema, so it grows whenever a schema is added.
    raise AssertionError(f"no store opener declared for {schema.value}")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _column_names(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The column names of ``table``, in SQLite's reported order, not as a set: a
    migration that reorders columns is real drift, and the manifest declares an order
    to compare against.
    """
    return tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _manifest_columns(schema: StoreSchema) -> tuple[str, ...]:
    """The column names the current manifest declares, in its own order."""
    return tuple(column.name for column in CURRENT_SCHEMAS[schema].columns)


def _applied_migration_ids(conn: sqlite3.Connection) -> set[str]:
    """The migration ids the ledger says are applied to this database."""
    return {str(row[0]) for row in conn.execute("SELECT migration_id FROM _yoyo_migration")}


def _assert_migration_recorded(conn: sqlite3.Connection, schema: StoreSchema) -> None:
    assert MIGRATION_IDS[schema] in _applied_migration_ids(conn)


def _stamped_legacy_job(db_path: Path) -> tuple[JobStore, CrawlJobState | None]:
    """Create a legacy jobs database, stamp it on open, and load the row back. Both
    legacy-stamping tests run these steps in one order, so the stamp is written once
    here -- inline they read as four identical lines.
    """
    _create_legacy_database(db_path, StoreSchema.CRAWL_JOBS)
    store = job_store(db_path)
    return store, store.load("legacy-job")


def _assert_unstamped(db_path: Path) -> None:
    """A freshly-created legacy database carries no migration record yet. Asserted once
    here: every legacy-stamping test needs the same precondition, and the two that
    restated it could disagree about what a legacy database is.
    """
    before = sqlite3.connect(db_path)
    assert "_yoyo_migration" not in _table_names(before)
    before.close()


def _assert_reopen_refused(db_path: Path, ddl: str, match: str) -> None:
    """Tamper with a stamped store's shape, then require the reopen to refuse it. The
    refusal is the point, the tamper setup: per-test it was connect/DDL/commit/close
    plus a ``pytest.raises`` whose ``match`` could name a column the DDL never dropped.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.commit()
    conn.close()
    with pytest.raises(Exception, match=match):
        job_store(db_path)


def _create_legacy_database(db_path: Path, schema: StoreSchema) -> None:
    table = CURRENT_SCHEMAS[schema]
    conn = sqlite3.connect(db_path)
    if schema is StoreSchema.CRAWL_JOBS:
        # A genuine pre-0003 database carries the columns the current manifest dropped. Order is irrelevant:
        # every write below names its columns.
        table = replace(table, columns=(*table.columns, *LEGACY_COLUMNS[StoreSchema.CRAWL_JOBS]))
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
                "[]",
                '["https://legacy.example"]',
                "[]",
                '{"https://legacy.example": 1}',
                '[{"url": "https://legacy.example/page", "status": "success"}]',
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
    _assert_unstamped(db_path)


def _table_with_column(table: TableSchema, column: ColumnSchema) -> TableSchema:
    return replace(
        table,
        columns=tuple(column if current.name == column.name else current for current in table.columns),
    )


def _rewrite_store_table(db_path: Path, table: TableSchema) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(f'ALTER TABLE "{table.name}" RENAME TO "{table.name}_old"')
    conn.execute(create_table_sql(table))
    conn.execute(f'DROP TABLE "{table.name}_old"')
    conn.commit()
    conn.close()


@pytest.mark.parametrize("schema", list(StoreSchema))
def test_fresh_database_is_empty_and_current_after_migrations(tmp_path, schema):
    db_path = tmp_path / f"{schema.value}.db"
    store = _open_store(schema, db_path)
    store.close()

    conn = sqlite3.connect(db_path)
    assert CURRENT_SCHEMAS[schema].table_name in _table_names(conn)
    assert schema_drift(conn, CURRENT_SCHEMAS[schema], schema) == ()
    assert conn.execute(f"SELECT COUNT(*) FROM {CURRENT_SCHEMAS[schema].table_name}").fetchone()[0] == 0
    assert _BOOKKEEPING_TABLES <= _table_names(conn)
    _assert_migration_recorded(conn, schema)
    conn.close()


def test_migration_apply_does_not_resolve_hostnames_per_migration(tmp_path, monkeypatch):
    """Migration logging must not reverse-resolve DNS per applied migration."""

    def refused_getfqdn(name=None):
        raise AssertionError("socket.getfqdn() called during migration apply")

    monkeypatch.setattr("socket.getfqdn", refused_getfqdn)
    db_path = tmp_path / "jobs.db"
    migrate_store(db_path, StoreSchema.CRAWL_JOBS)
    conn = sqlite3.connect(db_path)
    recorded = conn.execute("SELECT COUNT(*) FROM _yoyo_log").fetchone()[0]
    assert recorded > 0
    conn.close()


def test_legacy_job_database_is_stamped_without_losing_rows_or_columns(tmp_path):
    db_path = tmp_path / "legacy-jobs.db"
    store, loaded = _stamped_legacy_job(db_path)

    assert loaded is not None
    assert loaded.root_url == "https://legacy.example"
    assert loaded.status is JobStatus.PAUSED
    assert loaded.visited == {canonical("https://legacy.example")}
    assert loaded.results == [{"url": "https://legacy.example/page", "status": "success"}]
    assert loaded.transient_retries == {canonical("https://legacy.example"): 1}
    conn = sqlite3.connect(db_path)
    assert schema_drift(conn, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]) == ()
    assert _column_names(conn, "crawl_jobs") == _manifest_columns(StoreSchema.CRAWL_JOBS)
    _assert_migration_recorded(conn, StoreSchema.CRAWL_JOBS)
    conn.close()
    store.close()


def test_historical_canonicalized_column_is_removed_during_legacy_stamping(tmp_path):
    db_path = tmp_path / "historical-jobs.db"
    # The fixture's legacy shape already carries `canonicalized`: it is a LEGACY_COLUMN, and a database from
    # before 0002/0003 had all three.
    store, loaded = _stamped_legacy_job(db_path)
    after = sqlite3.connect(db_path)
    columns = set(_column_names(after, "crawl_jobs"))

    assert loaded is not None
    assert "canonicalized" not in columns
    assert schema_drift(after, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]) == ()
    _assert_migration_recorded(after, StoreSchema.CRAWL_JOBS)
    after.close()
    store.close()


def test_stamp_refuses_when_companion_tables_are_missing(tmp_path):
    """A complete crawl_jobs row must not be stamped through the migrations whose
    companion tables (0003's crawl_job_results/crawl_job_visited) do not exist:
    doing so boot-loops connect_store (#233)."""
    db_path = tmp_path / "missing-companions.db"
    conn = sqlite3.connect(db_path)
    conn.execute(create_table_sql(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]))
    conn.commit()
    conn.close()

    stamp_store(db_path, StoreSchema.CRAWL_JOBS)

    after = sqlite3.connect(db_path)
    applied = _applied_migration_ids(after)
    assert "0003_crawl_jobs_split_accumulators" not in applied, applied
    assert "0004_crawl_jobs_add_owner" not in applied, applied
    assert "0005_crawl_jobs_add_deleted_at" not in applied, applied
    after.close()


def test_explicit_legacy_stamp_records_only_satisfied_migration(tmp_path):
    db_path = tmp_path / "stamped-historical-jobs.db"
    _create_legacy_database(db_path, StoreSchema.CRAWL_JOBS)

    stamp_store(db_path, StoreSchema.CRAWL_JOBS)

    after = sqlite3.connect(db_path)
    columns = set(_column_names(after, "crawl_jobs"))
    applied = _applied_migration_ids(after)
    assert "canonicalized" in columns
    assert "0001_crawl_jobs_initial" in applied
    assert "0002_crawl_jobs_drop_canonicalized" not in applied
    assert schema_drift(after, CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS])
    after.close()


def test_explicit_legacy_stamp_reports_only_the_marked_migration(tmp_path, capsys):
    db_path = tmp_path / "cli-stamped-historical-jobs.db"
    _create_legacy_database(db_path, StoreSchema.CRAWL_JOBS)

    from crawlutron.storage.migrations.runner import main

    main(["stamp", str(db_path), StoreSchema.CRAWL_JOBS.value])

    output = capsys.readouterr().out
    assert "0001_crawl_jobs_initial" in output
    assert "0002_crawl_jobs_drop_canonicalized" not in output


@pytest.mark.parametrize(
    "changed_column",
    [
        replace(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS].columns[0], sqlite_type="BLOB"),
        replace(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS].columns[1], not_null=False),
        replace(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS].columns[1], default="'single_shot'"),
        replace(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS].columns[0], primary_key_position=0),
    ],
    ids=("type", "nullability", "default", "primary-key"),
)
def test_store_rejects_out_of_band_column_drift(tmp_path, changed_column):
    db_path = tmp_path / f"drift-{changed_column.name}.db"
    store = job_store(db_path)
    store.close()
    _rewrite_store_table(
        db_path,
        _table_with_column(CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS], changed_column),
    )

    with pytest.raises(LegacySchemaMismatch, match=changed_column.name):
        job_store(db_path)


def test_store_accepts_reordered_columns_at_startup(tmp_path):
    db_path = tmp_path / "reordered.db"
    table = CURRENT_SCHEMAS[StoreSchema.CRAWL_JOBS]
    store = job_store(db_path)
    store.close()
    reordered = replace(table, columns=tuple(reversed(table.columns)))
    _rewrite_store_table(db_path, reordered)

    reopened = job_store(db_path)
    reopened.close()
    conn = sqlite3.connect(db_path)
    actual = _column_names(conn, "crawl_jobs")
    conn.close()
    assert actual == tuple(column.name for column in reordered.columns)


def test_legacy_result_database_is_stamped_without_losing_rows_or_columns(tmp_path):
    db_path = tmp_path / "legacy-results.db"
    _create_legacy_database(db_path, StoreSchema.RESULTS)

    store = CrawlResultStore(db_path, ttl_seconds=0)

    assert store.get("legacy-request") == {"request_id": "legacy-request", "results": []}
    conn = sqlite3.connect(db_path)
    assert schema_drift(conn, CURRENT_SCHEMAS[StoreSchema.RESULTS]) == ()
    assert _column_names(conn, "results") == _manifest_columns(StoreSchema.RESULTS)
    _assert_migration_recorded(conn, StoreSchema.RESULTS)
    conn.close()
    store.close()


def test_missing_migration_column_is_refused_when_store_reopens(tmp_path):
    db_path = tmp_path / "missing-column.db"
    store = job_store(db_path)
    store.close()
    _assert_reopen_refused(
        db_path, "ALTER TABLE crawl_jobs DROP COLUMN transient_retries", match="transient_retries"
    )


def test_out_of_band_extra_column_is_refused_when_store_reopens(tmp_path):
    db_path = tmp_path / "extra-column.db"
    store = job_store(db_path)
    store.close()
    _assert_reopen_refused(db_path, "ALTER TABLE crawl_jobs ADD COLUMN unexpected TEXT", match="unexpected")


def test_out_of_band_changed_default_is_detected_as_drift(tmp_path):
    db_path = tmp_path / "changed-default.db"
    store = job_store(db_path)
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
    store = job_store(tmp_path / "jobs.db")
    result_store = CrawlResultStore(tmp_path / "results.db")
    job = store.create("https://example.test", CrawlPolicy())
    job.results = [{"url": "https://example.test", "status": "success"}]
    store.save(job)
    result_store.put("request-1", {"request_id": "request-1", "results": []})

    jobs = store.list_jobs()
    result = result_store.get("request-1")
    wire_payload = json.dumps({"jobs": jobs, "result": result}, sort_keys=True)

    assert jobs[0]["results_count"] == 1
    for table_name in _BOOKKEEPING_TABLES:
        assert table_name not in wire_payload
    store.close()
    result_store.close()


@pytest.mark.parametrize("schema", list(StoreSchema))
def test_migration_rollback_is_reversible_and_updates_the_ledger(tmp_path, schema):
    db_path = tmp_path / f"rollback-{schema.value}.db"
    store = _open_store(schema, db_path)
    store.close()
    rollback_store(db_path, schema)

    conn = sqlite3.connect(db_path)
    assert CURRENT_SCHEMAS[schema].table_name not in _table_names(conn)
    applied = _applied_migration_ids(conn)
    assert MIGRATION_IDS[schema] not in applied
    conn.close()
