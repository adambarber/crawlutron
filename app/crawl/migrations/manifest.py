"""Declared store schemas and the DDL rendered from that declaration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from app.crawl.sqlite_store import table_columns, table_indexes


class StoreSchema(StrEnum):
    CRAWL_JOBS = "crawl_jobs"
    RESULTS = "results"
    COORDINATION_COUNTERS = "coordination_counters"
    COORDINATION_COOKIES = "coordination_cookies"


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    sqlite_type: str
    not_null: bool
    default: str | None
    primary_key_position: int = 0


@dataclass(frozen=True)
class IndexSchema:
    name: str
    columns: tuple[str, ...]
    unique: bool = False
    partial: str | None = None


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    indexes: tuple[IndexSchema, ...] = ()

    @property
    def table_name(self) -> str:
        return self.name


CRAWL_JOBS_SCHEMA = TableSchema(
    name="crawl_jobs",
    columns=(
        ColumnSchema("job_id", "TEXT", False, None, 1),
        ColumnSchema("kind", "TEXT", True, "'whole_site'"),
        ColumnSchema("root_url", "TEXT", True, "''"),
        ColumnSchema("policy", "TEXT", True, "'{}'"),
        ColumnSchema("status", "TEXT", True, None),
        # The outstanding work queue, and only that. `visited` and `results`
        # used to sit here as JSON columns holding the job's whole history,
        # which made every batch persist re-serialize it; they are companion
        # tables now (see COMPANION_SCHEMAS). `frontier` stays a column
        # because it mutates as a queue rather than accumulating.
        ColumnSchema("frontier", "TEXT", True, "'[]'"),
        ColumnSchema("in_flight", "TEXT", True, "'[]'"),
        ColumnSchema("transient_retries", "TEXT", True, "'{}'"),
        ColumnSchema("created_at", "REAL", True, None),
        ColumnSchema("updated_at", "REAL", True, None),
        ColumnSchema("error", "TEXT", False, None),
        ColumnSchema("stats", "TEXT", True, "'{}'"),
    ),
)

# The two accumulators, split out of the job row. `seq` is the page's position
# in the crawl, which is also its identity here: results are appended and never
# rewritten, so the ordinal is both the insert key and the read order, and a
# batch persist costs one INSERT per new page instead of a full rewrite.
CRAWL_JOB_RESULTS_SCHEMA = TableSchema(
    name="crawl_job_results",
    columns=(
        ColumnSchema("job_id", "TEXT", False, None, 1),
        ColumnSchema("seq", "INTEGER", False, None, 2),
        ColumnSchema("payload", "TEXT", True, None),
    ),
)

# `visited` is a set, so the URL is the whole key and an insert of a page that
# was already seen is a no-op rather than a duplicate row.
CRAWL_JOB_VISITED_SCHEMA = TableSchema(
    name="crawl_job_visited",
    columns=(
        ColumnSchema("job_id", "TEXT", False, None, 1),
        ColumnSchema("url", "TEXT", False, None, 2),
    ),
)

# Tables that belong to a store schema's lifecycle but are not that schema's
# single primary table. They are created by the same migration chain, checked by
# the same drift check, and dropped with the same rollback, so a store cannot be
# half-migrated: there is one declaration of what a job store must contain.
COMPANION_SCHEMAS: dict[StoreSchema, tuple[TableSchema, ...]] = {
    StoreSchema.CRAWL_JOBS: (CRAWL_JOB_RESULTS_SCHEMA, CRAWL_JOB_VISITED_SCHEMA),
}

RESULTS_SCHEMA = TableSchema(
    name="results",
    columns=(
        ColumnSchema("request_id", "TEXT", False, None, 1),
        ColumnSchema("stored_at", "REAL", True, None),
        ColumnSchema("payload", "TEXT", True, None),
    ),
)

COORDINATION_COUNTERS_SCHEMA = TableSchema(
    name="coordination_counters",
    columns=(
        ColumnSchema("name", "TEXT", False, None, 1),
        # NOT NULL with a default so a row can never exist without a value. The
        # increment is atomic because the upsert names the column in its SET
        # list (see CoordinationCounters.add), not because of this default.
        ColumnSchema("value", "REAL", True, "0"),
    ),
)

COORDINATION_COOKIES_SCHEMA = TableSchema(
    name="coordination_cookies",
    columns=(
        ColumnSchema("namespace", "TEXT", False, None, 1),
        ColumnSchema("host", "TEXT", False, None, 2),
        # One JSON object per (namespace, host). Cookies for a host are few and
        # always rewritten together, so a row is the natural unit; a row per
        # cookie would only add a join to answer a question the header already
        # answers in one read.
        ColumnSchema("payload", "TEXT", True, "'{}'"),
        # LRU clock. The jar is long-lived and every crawled host adds a slot,
        # so the table is bounded the same way the in-memory map was.
        ColumnSchema("touched_at", "REAL", True, "0"),
    ),
)

CURRENT_SCHEMAS = {
    StoreSchema.CRAWL_JOBS: CRAWL_JOBS_SCHEMA,
    StoreSchema.RESULTS: RESULTS_SCHEMA,
    StoreSchema.COORDINATION_COUNTERS: COORDINATION_COUNTERS_SCHEMA,
    StoreSchema.COORDINATION_COOKIES: COORDINATION_COOKIES_SCHEMA,
}

LEGACY_COLUMNS = {
    StoreSchema.CRAWL_JOBS: (
        ColumnSchema("canonicalized", "INTEGER", True, "0"),
        # Present only on a database that has not run 0003 yet. They are
        # accepted rather than ignored so the stamping path can bring such a
        # database forward to the current schema instead of refusing to start.
        ColumnSchema("visited", "TEXT", True, "'[]'"),
        ColumnSchema("results", "TEXT", True, "'[]'"),
    ),
    StoreSchema.RESULTS: (),
    StoreSchema.COORDINATION_COUNTERS: (),
    StoreSchema.COORDINATION_COOKIES: (),
}

MIGRATION_SEQUENCES = {
    StoreSchema.CRAWL_JOBS: (
        "0001_crawl_jobs_initial",
        "0002_crawl_jobs_drop_canonicalized",
        "0003_crawl_jobs_split_accumulators",
    ),
    StoreSchema.RESULTS: ("0001_results_initial",),
    StoreSchema.COORDINATION_COUNTERS: ("0001_coordination_counters_initial",),
    StoreSchema.COORDINATION_COOKIES: ("0001_coordination_cookies_initial",),
}

MIGRATION_IDS = {
    schema: sequence[-1] for schema, sequence in MIGRATION_SEQUENCES.items()
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def create_table_sql(schema: TableSchema) -> str:
    key_columns = [column for column in schema.columns if column.primary_key_position]
    declarations: list[str] = []
    for column in schema.columns:
        declaration = f"  {_quote_identifier(column.name)} {column.sqlite_type}"
        # A composite key cannot be expressed inline -- SQLite rejects more
        # than one column-level PRIMARY KEY -- so it becomes a table constraint
        # below. A single-column key keeps the inline form, leaving the DDL of
        # the tables already written unchanged under a new renderer.
        if column.primary_key_position and len(key_columns) == 1:
            declaration += " PRIMARY KEY"
        if column.not_null:
            declaration += " NOT NULL"
        if column.default is not None:
            declaration += f" DEFAULT {column.default}"
        declarations.append(declaration)
    body = ",\n".join(declarations)
    quoted_keys = ", ".join(_quote_identifier(column.name) for column in key_columns)
    if len(key_columns) > 1:
        return f"CREATE TABLE {_quote_identifier(schema.name)} (\n{body},\n  PRIMARY KEY ({quoted_keys})\n)"
    return f"CREATE TABLE {_quote_identifier(schema.name)} (\n{body}\n)"


def _actual_columns(conn: sqlite3.Connection, table_name: str) -> tuple[ColumnSchema, ...]:
    return tuple(
        ColumnSchema(
            name=column.name,
            sqlite_type=column.sqlite_type,
            not_null=bool(column.not_null),
            default=column.default,
            primary_key_position=column.primary_key_position,
        )
        for column in table_columns(conn, table_name)
    )


def _actual_indexes(conn: sqlite3.Connection, table_name: str) -> tuple[IndexSchema, ...]:
    return tuple(
        sorted(
            (
                IndexSchema(
                    name=index.name,
                    columns=index.columns,
                    unique=bool(index.unique),
                    partial=index.where,
                )
                for index in table_indexes(conn, table_name)
                if index.origin != "pk"
            ),
            key=lambda index: index.name,
        )
    )


def _index_drift(conn: sqlite3.Connection, schema: TableSchema) -> tuple[str, ...]:
    actual = _actual_indexes(conn, schema.name)
    expected = tuple(sorted(schema.indexes, key=lambda index: index.name))
    if actual == expected:
        return ()
    return (f"indexes: expected={expected!r} actual={actual!r}",)


def _column_drift(
    conn: sqlite3.Connection,
    schema: TableSchema,
    *,
    allowed: dict[str, ColumnSchema],
) -> tuple[str, ...]:
    """Compare a live table against the declared manifest BY COLUMN NAME.

    SQLite appends a column added by ALTER TABLE to the end of the table, so a
    database created by the previous migration path has exactly the declared
    columns in a different ORDER. Order is a physical accident of how the table
    was built, not part of the contract -- SQL addresses columns by name. A
    positional comparison therefore rejects every database this service has
    ever created, which is how a startup conformance check becomes an outage.

    `allowed` carries columns that are permitted to exist beyond the manifest
    (a legacy column on the stamping path); they are still checked against their
    own declared shape, never ignored.
    """
    actual = {column.name: column for column in _actual_columns(conn, schema.name)}
    expected = {column.name: column for column in schema.columns}
    drift: list[str] = []
    for name in sorted(expected.keys() - actual.keys()):
        drift.append(f"missing column: {name}")
    for name in sorted(actual.keys() - expected.keys() - allowed.keys()):
        drift.append(f"unknown column: {name}")
    for name in sorted(actual.keys() & expected.keys()):
        if actual[name] != expected[name]:
            drift.append(f"column {name}: expected={expected[name]!r} actual={actual[name]!r}")
    for name in sorted(actual.keys() & allowed.keys()):
        if actual[name] != allowed[name]:
            drift.append(f"legacy column {name}: expected={allowed[name]!r} actual={actual[name]!r}")
    drift.extend(_index_drift(conn, schema))
    return tuple(drift)


def schema_drift(
    conn: sqlite3.Connection, schema: TableSchema, store_schema: StoreSchema | None = None
) -> tuple[str, ...]:
    """How a live table differs from the declaration, plus its companions' drift.

    ``store_schema`` is what lets a check cover the tables a store owns beyond
    its own: pass the owning schema and the companion tables are held to their
    declared shape too, so a store cannot be half-migrated. Omit it when only
    one table is under inspection.
    """
    drift = _column_drift(conn, schema, allowed={})
    if store_schema is not None:
        drift += _companion_drift(conn, store_schema)
    return drift


def _companion_drift(
    conn: sqlite3.Connection, store_schema: StoreSchema
) -> tuple[str, ...]:
    """Check the tables a store schema owns beyond its own primary table.

    Absent companions are not drift at that point in the chain: before
    0003 runs, `crawl_job_results` and `crawl_job_visited` correctly do not
    exist. Once they do, they are held to the declared shape exactly like the
    primary table, so a table that drifted is rejected rather than accepted
    because the store that owns it also owns another one.
    """
    drift: list[str] = []
    for companion in COMPANION_SCHEMAS.get(store_schema, ()):
        if not _table_named(conn, companion.name):
            continue
        drift.extend(
            f"companion {companion.name}: {item}" for item in _column_drift(conn, companion, allowed={})
        )
    return tuple(drift)


def _table_named(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def legacy_stamp_drift(
    conn: sqlite3.Connection,
    schema: TableSchema,
    store_schema: StoreSchema,
) -> tuple[str, ...]:
    return _column_drift(
        conn,
        schema,
        allowed={column.name: column for column in LEGACY_COLUMNS[store_schema]},
    )
