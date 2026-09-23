"""Yoyo migration runner for the service's SQLite stores."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from yoyo import get_backend, read_migrations
from yoyo.backends import DatabaseBackend, SQLiteBackend
from yoyo.connections import DatabaseURI
from yoyo.migrations import MigrationList, default_migration_table

from app.crawl.migrations.manifest import (
    CURRENT_SCHEMAS,
    MIGRATION_IDS,
    MIGRATION_SEQUENCES,
    StoreSchema,
    legacy_stamp_drift,
)
from app.crawl.sqlite_store import table_has_column

_MIGRATIONS_ROOT = Path(__file__).parent


class LegacySchemaMismatch(RuntimeError):
    """An unmarked table exists but does not match the current declared schema."""


class _MemorySQLiteBackend(SQLiteBackend):
    def connect(self, dburi: DatabaseURI) -> sqlite3.Connection:
        conn = self.driver.connect(
            f"file:{dburi.database}?mode=memory&cache=shared",
            uri=True,
            detect_types=self.driver.PARSE_DECLTYPES,
        )
        conn.isolation_level = None
        return conn


def _schema(value: StoreSchema | str) -> StoreSchema:
    return StoreSchema(value)


def _backend(db_path: str | Path, memory_name: str | None) -> DatabaseBackend:
    if memory_name is not None:
        uri = DatabaseURI("sqlite", None, None, None, None, memory_name, {})
        backend = _MemorySQLiteBackend(uri, default_migration_table)
        backend.init_database()
        return backend
    path = Path(db_path).resolve()
    return get_backend(f"sqlite:///{path.as_posix()}")


@contextmanager
def _locked_backend(
    db_path: str | Path,
    memory_name: str | None = None,
) -> Iterator[DatabaseBackend]:
    backend = _backend(db_path, memory_name)
    with backend:
        with backend.lock():
            yield backend


def _migrations(schema: StoreSchema) -> MigrationList:
    discovered = read_migrations(str(_MIGRATIONS_ROOT))
    expected = set(MIGRATION_SEQUENCES[schema])
    selected = [migration for migration in discovered if migration.id in expected]
    if {migration.id for migration in selected} != expected:
        found = ", ".join(migration.id for migration in selected)
        raise RuntimeError(f"missing migrations for {schema.value}: {found}")
    return MigrationList(selected, discovered.post_apply)


def _table_exists(conn: sqlite3.Connection, schema: StoreSchema) -> bool:
    table = CURRENT_SCHEMAS[schema]
    return any(table_has_column(conn, table.name, column.name) for column in table.columns)


def _require_stampable_schema(conn: sqlite3.Connection, schema: StoreSchema) -> None:
    table = CURRENT_SCHEMAS[schema]
    drift = legacy_stamp_drift(conn, table, schema)
    if drift:
        raise LegacySchemaMismatch(
            f"{CURRENT_SCHEMAS[schema].name} does not match the declared schema: {'; '.join(drift)}"
        )


def migrate_store(
    db_path: str | Path,
    schema: StoreSchema | str,
    *,
    memory_name: str | None = None,
) -> None:
    """Apply pending migrations, stamping an exact pre-ledger table when present."""
    selected = _schema(schema)
    migrations = _migrations(selected)
    with _locked_backend(db_path, memory_name) as backend:
        applied_hashes = set(backend.get_applied_migration_hashes())
        pending = backend.to_apply(migrations)
        if not pending:
            return
        target_was_applied = any(migration.hash in applied_hashes for migration in migrations)
        if not target_was_applied and _table_exists(backend.connection, selected):
            _require_stampable_schema(backend.connection, selected)
            initial = next(
                migration
                for migration in migrations
                if migration.id == MIGRATION_SEQUENCES[selected][0]
            )
            backend.mark_migrations([initial])
            remaining = backend.to_apply(migrations)
            if remaining:
                backend.apply_migrations(remaining)
        else:
            backend.apply_migrations(pending)


def stamp_store(db_path: str | Path, schema: StoreSchema | str) -> None:
    """Mark an exact pre-ledger schema as current without executing its migration."""
    selected = _schema(schema)
    migrations = _migrations(selected)
    with _locked_backend(db_path) as backend:
        if not _table_exists(backend.connection, selected):
            raise LegacySchemaMismatch(
                f"cannot stamp missing table {CURRENT_SCHEMAS[selected].name}"
            )
        _require_stampable_schema(backend.connection, selected)
        backend.mark_migrations(backend.to_apply(migrations))


def rollback_store(db_path: str | Path, schema: StoreSchema | str) -> None:
    """Rollback every applied migration for ``schema`` in reverse order."""
    selected = _schema(schema)
    migrations = _migrations(selected)
    with _locked_backend(db_path) as backend:
        backend.rollback_migrations(backend.to_rollback(migrations))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.crawl.migrations")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("apply", "stamp", "rollback"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("database", type=Path)
        command_parser.add_argument("schema", choices=[schema.value for schema in StoreSchema])
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    schema = _schema(args.schema)
    operations = {
        "apply": migrate_store,
        "stamp": stamp_store,
        "rollback": rollback_store,
    }
    operations[args.command](args.database, schema)
    print(f"{args.command} {MIGRATION_IDS[schema]} on {args.database}")
