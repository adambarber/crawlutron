"""Safe read/write/update/destroy primitives on one shared SQLite connection."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from crawlutron.runtime import process_state


class StorageError(Exception):
    """Base for every storage write-door error."""


class StorageBusy(StorageError):
    """BEGIN IMMEDIATE failed after the busy timeout; no silent retry."""


class StoreClosed(StorageError):
    """The store's connection is closed."""


class StoreConnectionPoisoned(StorageError):
    """COMMIT or ROLLBACK failed; the connection is fail-closed."""


class WriteRejected(StorageError):
    """A write was refused: loop thread, duplicate statement, or size cap."""


_WRITE_TXN = threading.local()


def _exec(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Execute one statement, translating a closed-connection error into StoreClosed."""
    try:
        return conn.execute(sql, params)
    except sqlite3.ProgrammingError as exc:
        raise StoreClosed(str(exc)) from exc


@contextmanager
def _transaction(
    conn: sqlite3.Connection,
    begin_sql: str,
    *,
    state: _TxnState | None,
) -> Generator[None, None, None]:
    """BEGIN ``begin_sql``, yield, roll back on failure, commit on success.

    ``state`` is marked poisoned when commit or rollback itself fails.
    """
    try:
        _exec(conn, begin_sql)
    except sqlite3.OperationalError as exc:
        raise StorageBusy(str(exc)) from exc
    try:
        yield
    except BaseException:
        try:
            _exec(conn, "ROLLBACK")
        except sqlite3.Error as exc:
            if state is not None:
                state.poisoned = True
            raise StoreConnectionPoisoned(str(exc)) from exc
        raise
    try:
        _exec(conn, "COMMIT")
    except sqlite3.Error as exc:
        if state is not None:
            state.poisoned = True
        raise StoreConnectionPoisoned(str(exc)) from exc


def _off_loop() -> None:
    """Raise when the current thread runs a running event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if getattr(_WRITE_TXN, "allow_depth", 0) == 0:
        raise WriteRejected(f"storage write on the event-loop thread ({loop!r}); use a store worker")


@contextmanager
def allow_on_loop() -> Generator[None, None, None]:
    """Temporarily permit loop-thread writes (lifespan bootstrap, shutdown sweeps)."""
    _WRITE_TXN.allow_depth = getattr(_WRITE_TXN, "allow_depth", 0) + 1
    try:
        yield
    finally:
        _WRITE_TXN.allow_depth = getattr(_WRITE_TXN, "allow_depth", 0) - 1


@dataclass
class _TxnState:
    active: bool = False
    owner: str = ""
    statements: set[str] = field(default_factory=set)
    rows_written: int = 0
    size_written: int = 0
    poisoned: bool = False


_CONN_STATE: dict[sqlite3.Connection, _TxnState] = {}
_CONN_STATE_LOCK = threading.Lock()


def _state_for(conn: sqlite3.Connection) -> _TxnState:
    with _CONN_STATE_LOCK:
        state = _CONN_STATE.get(conn)
        if state is None:
            state = _CONN_STATE[conn] = _TxnState()
        return state


def _drop_state(conn: sqlite3.Connection) -> None:
    with _CONN_STATE_LOCK:
        _CONN_STATE.pop(conn, None)


@contextmanager
def txn_write(conn: sqlite3.Connection, *, owner: str = "") -> Generator[None, None, None]:
    """BEGIN IMMEDIATE, commit, roll back on failure; one write txn per thread."""
    _off_loop()
    state = _state_for(conn)
    if state.active:
        raise WriteRejected(f"nested write transaction on {conn!r}; already open by {state.owner}")
    depth = getattr(_WRITE_TXN, "depth", 0)
    if depth > 0:
        raise WriteRejected("second concurrent write transaction on this thread")
    state.active = True
    state.owner = owner
    state.statements = set()
    state.rows_written = 0
    state.size_written = 0
    state.poisoned = False
    _WRITE_TXN.depth = depth + 1
    try:
        with _transaction(conn, "BEGIN IMMEDIATE", state=state):
            yield
    finally:
        _WRITE_TXN.depth = depth
        state.active = False
        state.owner = ""
        _drop_state(conn)


@contextmanager
def in_txn(conn: sqlite3.Connection) -> Generator[None, None, None]:
    """Assert an active transaction; run statements without opening one."""
    state = _state_for(conn)
    if not conn.in_transaction:
        raise WriteRejected("in_txn used outside an active transaction")
    if not state.active:
        raise WriteRejected("in_txn used outside a write transaction")
    try:
        yield
    except BaseException:
        raise


@contextmanager
def txn_read(conn: sqlite3.Connection) -> Generator[None, None, None]:
    """BEGIN DEFERRED, commit, roll back on failure for consistent reads."""
    with _transaction(conn, "BEGIN DEFERRED", state=None):
        yield


def wal_checkpoint(conn: sqlite3.Connection) -> None:
    """Truncate the WAL on a clean close."""
    _exec(conn, "PRAGMA wal_checkpoint(TRUNCATE)")


def _reset() -> None:
    """Drop every stored transaction state (test isolation)."""
    _CONN_STATE.clear()


def _has() -> bool:
    """Whether any transaction state is held right now."""
    return bool(_CONN_STATE)


process_state.register(__name__, _reset, _has)


__all__ = [
    "StorageBusy",
    "StorageError",
    "StoreClosed",
    "StoreConnectionPoisoned",
    "WriteRejected",
    "allow_on_loop",
    "in_txn",
    "txn_read",
    "txn_write",
    "wal_checkpoint",
]
