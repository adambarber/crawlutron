"""SQLite connection configuration and schema-introspection chokepoints."""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage.migrations.manifest import StoreSchema

_BUSY_TIMEOUT_MS = 5000

# `journal_mode=WAL` writes the header, so concurrent startups converting one fresh
# file get SQLITE_BUSY that busy_timeout never sees; a bounded retry covers the wait.
_WAL_RETRY_SECONDS = 15.0
_WAL_RETRY_BACKOFF_SECONDS = 0.05

#: The two bounds become ``configure_store_connection`` keyword arguments; the
#: module constants only hold the shipped defaults.


@dataclass(frozen=True)
class ColumnInfo:
    _cid: int
    name: str
    sqlite_type: str
    not_null: int
    default: object
    primary_key_position: int


@dataclass(frozen=True)
class IndexInfo:
    name: str
    unique: int
    columns: tuple[str, ...]
    origin: str
    where: str | None


def _set_busy_timeout(conn: sqlite3.Connection, milliseconds: int) -> None:
    """Set ``PRAGMA busy_timeout`` through the one chokepoint that splices a value in,
    through the same allowlist as every spliced fragment. A busy timeout is a
    non-negative millisecond integer; a negative/float/signed/dotted value is what SQLite misreads.
    """
    conn.execute(f"PRAGMA busy_timeout={_validated_identifier(str(milliseconds))}")


def configure_store_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    wal_retry_seconds: float = _WAL_RETRY_SECONDS,
) -> sqlite3.Connection:
    """Apply every connection-scoped service SQLite PRAGMA in this one function.

    ``busy_timeout_ms`` and ``wal_retry_seconds`` bound the WAL-conversion
    retry a caller tunes for a contended or already-configured store."""
    # `synchronous` stays at SQLite's FULL default: under 0.05ms/commit
    # here, so the fsync tax is immaterial (measured directly against this store).
    _set_busy_timeout(conn, busy_timeout_ms)
    _ensure_wal(conn, busy_timeout_ms=busy_timeout_ms, wal_retry_seconds=wal_retry_seconds)
    return conn


def _ensure_wal(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int,
    wal_retry_seconds: float,
) -> None:
    """Put the database in WAL, retrying while a sibling process holds the file.
    Each attempt runs under its own timeout OR the deadline's remainder --
    whichever is smaller -- and the connection's configured timeout is restored on the way out.
    """
    deadline = time.monotonic() + wal_retry_seconds
    try:
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            _set_busy_timeout(conn, max(1, min(busy_timeout_ms, remaining_ms)))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_WAL_RETRY_BACKOFF_SECONDS)
    finally:
        _set_busy_timeout(conn, busy_timeout_ms)


def _open_raw_store_connection(
    db_path: str | Path,
) -> tuple[sqlite3.Connection, str | None]:
    if str(db_path) == ":memory:":
        memory_name = f"crawlutron-{secrets.token_hex(16)}"
        conn = sqlite3.connect(
            f"file:{memory_name}?mode=memory&cache=shared",
            check_same_thread=False,
            uri=True,
        )
        return conn, memory_name
    return sqlite3.connect(str(db_path), check_same_thread=False), None


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def store_lock_key(db_path: str | Path, memory_name: str | None = None) -> str:
    """One lock key per database, however the caller spelled the path.
    The migration backend opens the *resolved* file, so a raw-path key would name
    a different lock (symlink is the ordinary case); two keys means no exclusion.
    """
    if memory_name is not None:
        return f"memory:{memory_name}"
    return str(Path(db_path).resolve())


def process_lock(key: str) -> threading.RLock:
    """The one lock every writer and every migration on this database takes.
    A store's own lock only serializes that instance, and a migration runs on its
    own connection; one per-file lock shared by both makes the collision unrepresentable.
    """
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = _PROCESS_LOCKS[key] = threading.RLock()
        return lock


def connect_store(db_path: str | Path, *, schema: StoreSchema) -> tuple[sqlite3.Connection, str | None]:
    """Open and configure the long-lived store connection, then migrate its database.

    The memory name is returned so the caller can lock on the identity the migration took.
    """
    # Service PRAGMAs come from ``configure_store_connection``; the migration's own
    # connection carries the same PRAGMAs, which is why writes serialize on the file's lock.
    conn, memory_name = _open_raw_store_connection(db_path)
    try:
        configure_store_connection(conn)
        from app.storage.migrations import migrate_store

        migrate_store(db_path, schema, memory_name=memory_name)
    except BaseException:
        conn.close()
        raise
    return conn, memory_name


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
        columns = tuple(str(index_row[2]) for index_row in conn.execute(f"PRAGMA index_info({index_name})"))
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


__all__ = [
    "ColumnInfo",
    "IndexInfo",
    "configure_store_connection",
    "connect_store",
    "process_lock",
    "store_lock_key",
    "table_columns",
    "table_has_column",
    "table_indexes",
]
