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
