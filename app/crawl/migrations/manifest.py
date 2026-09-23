"""Declared store schemas and the DDL rendered from that declaration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from app.crawl.sqlite_store import table_columns, table_indexes


class StoreSchema(StrEnum):
    CRAWL_JOBS = "crawl_jobs"
    RESULTS = "results"


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
        ColumnSchema("frontier", "TEXT", True, "'[]'"),
        ColumnSchema("visited", "TEXT", True, "'[]'"),
        ColumnSchema("in_flight", "TEXT", True, "'[]'"),
        ColumnSchema("transient_retries", "TEXT", True, "'{}'"),
        ColumnSchema("results", "TEXT", True, "'[]'"),
        ColumnSchema("created_at", "REAL", True, None),
        ColumnSchema("updated_at", "REAL", True, None),
        ColumnSchema("error", "TEXT", False, None),
        ColumnSchema("stats", "TEXT", True, "'{}'"),
    ),
)

RESULTS_SCHEMA = TableSchema(
    name="results",
    columns=(
        ColumnSchema("request_id", "TEXT", False, None, 1),
        ColumnSchema("stored_at", "REAL", True, None),
        ColumnSchema("payload", "TEXT", True, None),
    ),
)

CURRENT_SCHEMAS = {
    StoreSchema.CRAWL_JOBS: CRAWL_JOBS_SCHEMA,
    StoreSchema.RESULTS: RESULTS_SCHEMA,
}

LEGACY_COLUMNS = {
    StoreSchema.CRAWL_JOBS: (
        ColumnSchema("canonicalized", "INTEGER", True, "0"),
    ),
    StoreSchema.RESULTS: (),
}

MIGRATION_SEQUENCES = {
    StoreSchema.CRAWL_JOBS: ("0001_crawl_jobs_initial", "0002_crawl_jobs_drop_canonicalized"),
    StoreSchema.RESULTS: ("0001_results_initial",),
}

MIGRATION_IDS = {
    schema: sequence[-1] for schema, sequence in MIGRATION_SEQUENCES.items()
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def create_table_sql(schema: TableSchema) -> str:
    declarations: list[str] = []
    for column in schema.columns:
        declaration = f"  {_quote_identifier(column.name)} {column.sqlite_type}"
        if column.primary_key_position:
            declaration += " PRIMARY KEY"
        if column.not_null:
            declaration += " NOT NULL"
        if column.default is not None:
            declaration += f" DEFAULT {column.default}"
        declarations.append(declaration)
    body = ",\n".join(declarations)
    return f"CREATE TABLE {_quote_identifier(schema.name)} (\n{body}\n)"


def create_index_statements(schema: TableSchema) -> tuple[str, ...]:
    statements: list[str] = []
    for index in schema.indexes:
        unique = "UNIQUE " if index.unique else ""
        where = f" WHERE {index.partial}" if index.partial else ""
        columns = ", ".join(_quote_identifier(column) for column in index.columns)
        statements.append(
            f"CREATE {unique}INDEX {_quote_identifier(index.name)} "
            f"ON {_quote_identifier(schema.name)} ({columns}){where}"
        )
    return tuple(statements)


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


def schema_drift(conn: sqlite3.Connection, schema: TableSchema) -> tuple[str, ...]:
    actual_columns = _actual_columns(conn, schema.name)
    if actual_columns == schema.columns:
        return _index_drift(conn, schema)
    return (
        f"columns: expected={schema.columns!r} actual={actual_columns!r}",
        *_index_drift(conn, schema),
    )


def legacy_stamp_drift(
    conn: sqlite3.Connection,
    schema: TableSchema,
    store_schema: StoreSchema,
) -> tuple[str, ...]:
    actual = {column.name: column for column in _actual_columns(conn, schema.name)}
    expected = {column.name: column for column in schema.columns}
    allowed = {column.name: column for column in LEGACY_COLUMNS[store_schema]}
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
