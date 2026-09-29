"""SQLite connection configuration and schema-introspection chokepoints."""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.crawl.migrations.manifest import StoreSchema

_BUSY_TIMEOUT_MS = 5000

# `PRAGMA journal_mode=WAL` is a write to the database header, and converting a
# file that is not yet WAL needs a lock no other connection holds. When
# `uvicorn --workers` starts its children together, every one of them converts
# the same fresh database in the same instant; SQLite grants the lock to one and
# returns SQLITE_BUSY to the rest. `busy_timeout` does not cover this: measured
# with four spawned processes, three of four children failed 0.000s into the
# call, so the busy handler is never invoked. A retry is the only remedy for an
# error the library declines to wait out.
#
# The budget covers the cascade, not one lock: a loser waits while the winner
# runs its migrations, so the wait must outlast a peer's whole migration rather
# than a single header write. Bounded, because an unbounded retry turns a hard
# lock into a hang only an operator can clear.
_WAL_RETRY_SECONDS = 15.0
_WAL_RETRY_BACKOFF_SECONDS = 0.05


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


def _set_busy_timeout(conn: sqlite3.Connection, milliseconds: int) -> None:
    """Set ``PRAGMA busy_timeout`` through the one chokepoint that splices a value in.

    The pragma guard admits a module constant or a validated identifier and
    nothing else, and this statement needs a computed value, so the value goes
    through the same allowlist as every other spliced fragment instead of
    earning an exemption of its own. The allowlist is not ceremony here: a
    busy timeout is a non-negative integer of milliseconds, and the one thing
    that is not -- a negative, a float, anything with a sign or a dot -- is
    exactly what SQLite would misread.
    """
    conn.execute(f"PRAGMA busy_timeout={_validated_identifier(str(milliseconds))}")


def configure_store_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply every connection-scoped service SQLite PRAGMA in this one function."""
    _set_busy_timeout(conn, _BUSY_TIMEOUT_MS)
    _ensure_wal(conn)
    return conn


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """Put the database in WAL, retrying while a sibling process holds the file.

    On an already-WAL database the PRAGMA is a no-op read and never takes a
    write lock, so the steady state stays uncontended; the retry is reached
    only by the first conversion, which is the only time this is a write.

    The two budgets are different things and both are real: ``_BUSY_TIMEOUT_MS``
    is how long ONE attempt may block inside SQLite before it can report
    contention, and ``_WAL_RETRY_SECONDS`` is the total wait. Checking the
    deadline only between attempts is not enough to honour the second, because
    an attempt allowed the first budget can outlive the second on its own -- a
    15s budget over a 5s busy timeout really waits 20s, and a caller that
    shortened the retry to half a second still waited five. Each attempt
    therefore runs under whichever is smaller: its own timeout, or what is
    left of the deadline.

    The connection's configured timeout is restored on the way out, including
    the failure path: a caller set ``_BUSY_TIMEOUT_MS`` for every statement on
    this connection, and how long the WAL conversion happened to take is not
    a licence to leave it narrowed.
    """
    deadline = time.monotonic() + _WAL_RETRY_SECONDS
    try:
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            _set_busy_timeout(conn, max(1, min(_BUSY_TIMEOUT_MS, remaining_ms)))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_WAL_RETRY_BACKOFF_SECONDS)
    finally:
        _set_busy_timeout(conn, _BUSY_TIMEOUT_MS)


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
    "table_columns",
    "table_has_column",
    "table_indexes",
]
