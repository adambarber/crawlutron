"""SQLite connection configuration and schema-introspection chokepoints."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.crawl.migrations.manifest import StoreSchema

_BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class ColumnInfo:
    cid: int
    name: str
    sqlite_type: str
    not_null: int
    default: Any
    primary_key_position: int


@dataclass(frozen=True)
class IndexInfo:
    name: str
    unique: int
    columns: tuple[str, ...]
    origin: str
    where: str | None


def configure_store_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply every connection-scoped service SQLite PRAGMA in this one function."""
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _open_raw_store_connection(
    db_path: str | Path,
) -> tuple[sqlite3.Connection, str | None]:
    if str(db_path) == ":memory:":
        memory_name = f"browser-crawler-{secrets.token_hex(16)}"
        conn = sqlite3.connect(
            f"file:{memory_name}?mode=memory&cache=shared",
            check_same_thread=False,
            uri=True,
        )
        return conn, memory_name
    return sqlite3.connect(str(db_path), check_same_thread=False), None


def connect_store(db_path: str | Path, *, schema: StoreSchema) -> sqlite3.Connection:
    """Open and configure the long-lived store connection, then migrate its database.

    Service PRAGMAs are configured only by ``configure_store_connection`` on this
    connection. Yoyo uses a separate temporary DBAPI connection for ledger and
    migration work; it receives no PRAGMA configuration.
    """
    conn, memory_name = _open_raw_store_connection(db_path)
    try:
        configure_store_connection(conn)
        from app.crawl.migrations import migrate_store

        migrate_store(db_path, schema, memory_name=memory_name)
    except BaseException:
        conn.close()
        raise
    return conn


def _validated_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def table_columns(conn: sqlite3.Connection, table: str) -> tuple[ColumnInfo, ...]:
    """Return SQLite's ordered column metadata for ``table``."""
    table = _validated_identifier(table)
    return tuple(ColumnInfo(*row) for row in conn.execute(f"PRAGMA table_info({table})"))


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether ``table`` already has ``column``."""
    return any(info.name == column for info in table_columns(conn, table))


def table_indexes(conn: sqlite3.Connection, table: str) -> tuple[IndexInfo, ...]:
    """Return explicit and SQLite-generated index metadata for ``table``."""
    table = _validated_identifier(table)
    indexes: list[IndexInfo] = []
    for row in conn.execute(f"PRAGMA index_list({table})"):
        index_name = _validated_identifier(str(row[1]))
        columns = tuple(
            str(index_row[2])
            for index_row in conn.execute(f"PRAGMA index_info({index_name})")
        )
        indexes.append(
            IndexInfo(
                name=index_name,
                unique=int(row[2]),
                columns=columns,
                origin=str(row[3]),
                where=None if not row[4] else str(row[4]),
            )
        )
    return tuple(indexes)
