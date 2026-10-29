"""Shared DDL step builders for the migration files.

These helpers bind each manifest-rendered statement to its matching rollback, so
a migration file declares *which* schema it creates, never re-spells the SQL.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.storage.migrations.manifest import TableSchema, create_index_sql, create_table_sql
from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_has_column

__all__ = ["add_column_step", "table_steps"]


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_steps(schema: TableSchema) -> Iterable:
    """The step list a table-and-indexes migration creates: one step for the
    table (rollback drops it) and one per declared index (rollback drops it)."""
    table = step(create_table_sql(schema), f"DROP TABLE {_quote(schema.name)}")
    indexes = (
        step(create_index_sql(schema, index), f"DROP INDEX {_quote(index.name)}") for index in schema.indexes
    )
    return table, *indexes


def add_column_step(table_name: str, column_ddl: str) -> Iterable:
    """The add/drop-column pair guarded by ``table_has_column``: a legacy
    database already carrying the column no-ops the upgrade, and the rollback
    drops it only when the migration added it (the guard was re-spelled 3x)."""

    column_name = column_ddl.split()[0]

    def apply_step(conn) -> None:
        if not table_has_column(conn, table_name, column_name):
            conn.execute(f"ALTER TABLE {_quote(table_name)} ADD COLUMN {column_ddl}")

    def rollback_step(conn) -> None:
        if table_has_column(conn, table_name, column_name):
            conn.execute(f"ALTER TABLE {_quote(table_name)} DROP COLUMN {_quote(column_name)}")

    return [step(apply_step, rollback_step)]
