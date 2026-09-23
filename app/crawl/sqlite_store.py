"""The single durability configuration for every SQLite store in the service.

Connection settings (busy_timeout, journal_mode) are a service decision, not
a per-store one: CrawlResultStore once plain-connected while JobStore ran
WAL + busy_timeout, so the same process gave two stores different
concurrency behavior (M1). New stores call :func:`connect_store` instead of
``sqlite3.connect`` directly; the confinement invariant in
tests/test_store_pragma_chokepoint.py keeps every PRAGMA in this file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_BUSY_TIMEOUT_MS = 5000


def configure_store_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    # Wait for the write lock instead of raising "database is locked" under
    # concurrent access from FastAPI's worker threads.
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # WAL: better concurrent read/write performance for the read-heavy stores.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect_store(db_path: str | Path) -> sqlite3.Connection:
    """Open a store connection with the service-wide configuration applied."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return configure_store_connection(conn)


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether ``table`` already has ``column``.

    Schema introspection lives beside the PRAGMA chokepoint so a store never
    grows its own SQL vocabulary. Migrations use it to decide whether an ALTER
    is needed, which is how the job store stopped swallowing every
    ``OperationalError`` (a real failure looked identical to "already
    migrated"). ``table`` is a caller-supplied identifier, so it is checked
    against the identifier grammar rather than interpolated blind.
    """
    if not table.replace("_", "").isalnum():
        raise ValueError(f"unsafe table identifier: {table!r}")
    return any(
        row[1] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )
