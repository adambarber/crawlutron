"""Declared store schemas and the DDL rendered from that declaration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from app.storage.sqlite_store import table_columns, table_indexes


class StoreSchema(StrEnum):
    CRAWL_JOBS = "crawl_jobs"
    RESULTS = "results"
    COORDINATION_COUNTERS = "coordination_counters"
    COORDINATION_COOKIES = "coordination_cookies"
    WATCHES = "watches"
    CALLER_KEYS = "caller_keys"


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
        # The outstanding queue only: `visited`/`results` are companion tables now
        # (COMPANION_SCHEMAS); `frontier` mutates as a queue, so it stays a column.
        ColumnSchema("frontier", "TEXT", True, "'[]'"),
        ColumnSchema("in_flight", "TEXT", True, "'[]'"),
        ColumnSchema("transient_retries", "TEXT", True, "'{}'"),
        ColumnSchema("created_at", "REAL", True, None),
        ColumnSchema("updated_at", "REAL", True, None),
        ColumnSchema("error", "TEXT", False, None),
        ColumnSchema("stats", "TEXT", True, "'{}'"),
        ColumnSchema("owner", "TEXT", True, "''"),
        # Deleted-at tombstone: a timestamp hides the row and a late save no-ops.
        ColumnSchema("deleted_at", "REAL", False, None),
    ),
)

# The two accumulators, split from the job row: `seq` is the page's position and
# identity (appended, never rewritten), so a persist is one INSERT per new page.
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

# Tables in a store schema's lifecycle that are not its single primary table:
# created, drift-checked and rolled back together, so none can be half-migrated.
COMPANION_SCHEMAS: dict[StoreSchema, tuple[TableSchema, ...]] = {
    StoreSchema.CRAWL_JOBS: (CRAWL_JOB_RESULTS_SCHEMA, CRAWL_JOB_VISITED_SCHEMA),
}

RESULTS_SCHEMA = TableSchema(
    name="results",
    columns=(
        ColumnSchema("request_id", "TEXT", False, None, 1),
        ColumnSchema("stored_at", "REAL", True, None),
        ColumnSchema("payload", "TEXT", True, None),
        ColumnSchema("owner", "TEXT", True, "''"),
    ),
)

COORDINATION_COUNTERS_SCHEMA = TableSchema(
    name="coordination_counters",
    columns=(
        ColumnSchema("name", "TEXT", False, None, 1),
        # NOT NULL with a default, so a row never lacks a value; the increment is
        # atomic by the upsert's SET list (CoordinationCounters.add), not the default.
        ColumnSchema("value", "REAL", True, "0"),
    ),
)

COORDINATION_COOKIES_SCHEMA = TableSchema(
    name="coordination_cookies",
    columns=(
        ColumnSchema("namespace", "TEXT", False, None, 1),
        ColumnSchema("host", "TEXT", False, None, 2),
        # One JSON object per (namespace, host): cookies for a host are few and
        # always rewritten together, so one read answers the header question.
        ColumnSchema("payload", "TEXT", True, "'{}'"),
        # LRU clock. The jar is long-lived and every crawled host adds a slot,
        # so the table is bounded the same way the in-memory map was.
        ColumnSchema("touched_at", "REAL", True, "0"),
    ),
)

# One row per registered watch: a watch is a single URL and its revalidation
# history, rewritten per check -- no append-only page history to split out.
WATCHES_SCHEMA = TableSchema(
    name="watches",
    columns=(
        ColumnSchema("watch_id", "TEXT", False, None, 1),
        ColumnSchema("url", "TEXT", True, None),
        # NULL means a poll-only watch: registration does not require a
        # delivery destination, and the sweep fires nothing without one.
        ColumnSchema("webhook_url", "TEXT", False, None),
        ColumnSchema("interval_seconds", "INTEGER", True, None),
        ColumnSchema("status", "TEXT", True, "'active'"),
        # etag/last_modified exactly as ContentValidators spells them, so store
        # and wire share one vocabulary with no translation layer.
        ColumnSchema("validators", "TEXT", True, "'{}'"),
        ColumnSchema("next_check_at", "REAL", True, None),
        ColumnSchema("last_checked_at", "REAL", False, None),
        ColumnSchema("last_outcome", "TEXT", False, None),
        ColumnSchema("last_changed_at", "REAL", False, None),
        ColumnSchema("consecutive_failures", "INTEGER", True, "0"),
        ColumnSchema("created_at", "REAL", True, None),
        ColumnSchema("updated_at", "REAL", True, None),
        # Owner of the watch row: the anonymous bucket ("") or a keyed caller's id.
        # Registration, list, read, delete are owner-scoped; the due read stays global.
        ColumnSchema("owner", "TEXT", True, "''"),
    ),
    indexes=(
        # The due-claim sweep reads (status, next_check_at) and nothing else;
        # ``owner`` is absent so the due scan keeps using this index.
        IndexSchema("watches_due", ("status", "next_check_at")),
    ),
)

CALLER_KEYS_SCHEMA = TableSchema(
    name="caller_keys",
    columns=(
        ColumnSchema("key_id", "TEXT", False, None, 1),
        ColumnSchema("label", "TEXT", True, "''"),
        ColumnSchema("key_hash", "TEXT", True, None),
        ColumnSchema("max_pages", "INTEGER", True, "0"),
        ColumnSchema("max_concurrency", "INTEGER", True, "0"),
        ColumnSchema("created_at", "REAL", True, None),
    ),
    indexes=(IndexSchema("caller_keys_by_hash", ("key_hash",), unique=True),),
)

CURRENT_SCHEMAS = {
    StoreSchema.CRAWL_JOBS: CRAWL_JOBS_SCHEMA,
    StoreSchema.RESULTS: RESULTS_SCHEMA,
    StoreSchema.COORDINATION_COUNTERS: COORDINATION_COUNTERS_SCHEMA,
    StoreSchema.COORDINATION_COOKIES: COORDINATION_COOKIES_SCHEMA,
    StoreSchema.WATCHES: WATCHES_SCHEMA,
    StoreSchema.CALLER_KEYS: CALLER_KEYS_SCHEMA,
}

LEGACY_COLUMNS = {
    StoreSchema.CRAWL_JOBS: (
        ColumnSchema("canonicalized", "INTEGER", True, "0"),
        # Present only on a database that has not run 0003 yet; accepted so the
        # stamping path brings it forward instead of refusing to start.
        ColumnSchema("visited", "TEXT", True, "'[]'"),
        ColumnSchema("results", "TEXT", True, "'[]'"),
    ),
    StoreSchema.RESULTS: (),
    StoreSchema.COORDINATION_COUNTERS: (),
    StoreSchema.COORDINATION_COOKIES: (),
    StoreSchema.WATCHES: (),
    StoreSchema.CALLER_KEYS: (),
}

MIGRATION_SEQUENCES = {
    StoreSchema.CRAWL_JOBS: (
        "0001_crawl_jobs_initial",
        "0002_crawl_jobs_drop_canonicalized",
        "0003_crawl_jobs_split_accumulators",
        "0004_crawl_jobs_add_owner",
        "0005_crawl_jobs_add_deleted_at",
    ),
    StoreSchema.RESULTS: ("0001_results_initial", "0002_results_add_owner"),
    StoreSchema.COORDINATION_COUNTERS: ("0001_coordination_counters_initial",),
    StoreSchema.COORDINATION_COOKIES: ("0001_coordination_cookies_initial",),
    StoreSchema.WATCHES: ("0001_watches_initial", "0002_watches_add_owner"),
    StoreSchema.CALLER_KEYS: ("0001_caller_keys_initial",),
}

MIGRATION_IDS = {schema: sequence[-1] for schema, sequence in MIGRATION_SEQUENCES.items()}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def create_table_sql(schema: TableSchema) -> str:
    key_columns = [column for column in schema.columns if column.primary_key_position]
    declarations: list[str] = []
    for column in schema.columns:
        declaration = f"  {_quote_identifier(column.name)} {column.sqlite_type}"
        # A composite key cannot be inline (SQLite rejects multiple column-level
        # PRIMARY KEYs) -- a table constraint instead; single keys stay inline.
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


def create_index_sql(schema: TableSchema, index: IndexSchema) -> str:
    """Render an index's DDL from its declaration -- a migration creates indexes
    through this renderer rather than spelling DDL itself, so the declared index
    and the one the drift check compares cannot be two different statements.
    """
    unique = "UNIQUE " if index.unique else ""
    name = _quote_identifier(index.name)
    table = _quote_identifier(schema.name)
    columns = ", ".join(_quote_identifier(column) for column in index.columns)
    where = f" WHERE {index.partial}" if index.partial else ""
    return f"CREATE {unique}INDEX {name} ON {table} ({columns}){where}"


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

    SQLite appends ALTER TABLE columns to the end, so order is not a contract --
    positional comparison would reject every database this service has built.
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
    Pass ``store_schema`` to hold the tables a store owns beyond its own to their
    declared shape too -- so a store cannot be half-migrated; omit for one table.
    """
    drift = _column_drift(conn, schema, allowed={})
    if store_schema is not None:
        drift += _companion_drift(conn, store_schema)
    return drift


def _companion_drift(conn: sqlite3.Connection, store_schema: StoreSchema) -> tuple[str, ...]:
    """Check the tables a store schema owns beyond its primary table.

    An absent companion is drift too, so its creating migration cannot be
    marked applied without the tables -- the boot loop #233 closes ("""
    drift: list[str] = []
    for companion in COMPANION_SCHEMAS.get(store_schema, ()):
        if not _table_named(conn, companion.name):
            drift.append(f"companion {companion.name}: missing table")
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
