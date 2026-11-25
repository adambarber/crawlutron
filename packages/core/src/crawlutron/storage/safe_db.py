"""Safe read/write/update/destroy primitives on one shared SQLite connection."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast

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


class ReadRejected(StorageError):
    """A read was refused: loop thread, row cap, size cap, deadline, or data_version mismatch."""


_WRITE_TXN = threading.local()
_READ_TXN = threading.local()


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
) -> Generator[None]:
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


def assert_off_main_thread() -> None:
    """Raise ReadRejected when the current thread runs a running event loop without read allowance."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if getattr(_READ_TXN, "allow_depth", 0) == 0:
        raise ReadRejected(f"storage read on the event-loop thread ({loop!r}); use a store worker")


@contextmanager
def allow_on_loop() -> Generator[None]:
    """Temporarily permit loop-thread writes (lifespan bootstrap, shutdown sweeps)."""
    _WRITE_TXN.allow_depth = getattr(_WRITE_TXN, "allow_depth", 0) + 1
    try:
        yield
    finally:
        _WRITE_TXN.allow_depth = getattr(_WRITE_TXN, "allow_depth", 0) - 1


@contextmanager
def allow_on_loop_read() -> Generator[None]:
    """Temporarily permit loop-thread reads (bootstrap, shutdown sweeps)."""
    _READ_TXN.allow_depth = getattr(_READ_TXN, "allow_depth", 0) + 1
    try:
        yield
    finally:
        _READ_TXN.allow_depth = getattr(_READ_TXN, "allow_depth", 0) - 1


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
def txn_write(conn: sqlite3.Connection, *, owner: str = "") -> Generator[None]:
    """BEGIN IMMEDIATE, commit, roll back on failure; one write txn per thread."""
    _off_loop()
    state = _state_for(conn)
    if state.poisoned:
        raise StoreConnectionPoisoned(f"connection {conn!r} is poisoned; fail-closed")
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
        if not state.poisoned:
            _drop_state(conn)


@contextmanager
def in_txn(conn: sqlite3.Connection) -> Generator[None]:
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


def _row_byte_width(row) -> int:
    """Bytes a row actually carries: None is zero, blobs their length,
    text its utf-8 width, other scalars their utf-8 rendering."""
    total = 0
    for v in row:
        if v is None:
            continue
        if isinstance(v, (bytes, bytearray, memoryview)):
            total += len(v)
        elif isinstance(v, str):
            total += len(v.encode("utf-8"))
        else:
            total += len(str(v).encode("utf-8"))
    return total


def _apply_bounds(row, owner: str) -> None:
    """Accrue one row into every active bound and enforce each cap."""
    for ctx in getattr(_READ_TXN, "bound_stack", ()):
        ctx["rows"] += 1
        if ctx["max_rows"] is not None and ctx["rows"] > ctx["max_rows"]:
            raise ReadRejected(f"read row cap exceeded: {ctx['rows']} > {ctx['max_rows']}")
        ctx["bytes"] += _row_byte_width(row)
        if ctx["max_bytes"] is not None and ctx["bytes"] > ctx["max_bytes"]:
            raise ReadRejected(f"read size cap exceeded: {ctx['bytes']} > {ctx['max_bytes']}")
        if ctx["deadline"] is not None and time.monotonic() > ctx["deadline"]:
            raise ReadRejected(f"read deadline exceeded for {owner!r}")


@contextmanager
def txn_read(
    conn: sqlite3.Connection,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    deadline: float | None = None,
    owner: str = "",
) -> Generator[None]:
    """BEGIN DEFERRED for consistent reads.
    Reentrant: a nested call joins the outer snapshot; depth restored on every path.
    Bounds stack: the tightest active cap/deadline wins."""
    assert_off_main_thread()
    state = _state_for(conn)
    if state.poisoned:
        raise StoreConnectionPoisoned(f"connection {conn!r} is poisoned; fail-closed")
    depth = getattr(_READ_TXN, "depth", 0)
    outer_conn = getattr(_READ_TXN, "conn", None)
    if deadline is not None and time.monotonic() > deadline:
        raise ReadRejected(f"read deadline exceeded for {owner!r}")
    bounded = max_rows is not None or max_bytes is not None or deadline is not None
    context = None
    stack = None
    if bounded:
        context = {"rows": 0, "bytes": 0, "max_rows": max_rows, "max_bytes": max_bytes, "deadline": deadline}
        stack = cast(list, getattr(_READ_TXN, "bound_stack", None))
        if stack is None:
            stack = []
            _READ_TXN.bound_stack = stack
        stack.append(context)
    if depth > 0:
        if outer_conn is conn:
            _READ_TXN.depth = depth + 1
            try:
                yield
            finally:
                _READ_TXN.depth = depth
                if context is not None and stack is not None:
                    stack.pop()
            return
        raise ReadRejected("second concurrent read transaction on this thread")
    with _read_fresh(conn, owner, context, stack):
        yield


@contextmanager
def _read_fresh(
    conn: sqlite3.Connection,
    owner: str,
    context: dict | None,
    stack: list | None,
) -> Generator[None]:
    """Run one fresh read transaction (depth == 0): counting row factory + txn."""
    _READ_TXN.depth = 1
    _READ_TXN.conn = conn

    original_row_factory = conn.row_factory

    def counting_row_factory(_cursor, row):
        _apply_bounds(row, owner)
        if original_row_factory is not None:
            return original_row_factory(_cursor, row)
        return row

    if context is not None:
        conn.row_factory = counting_row_factory
    try:
        with _transaction(conn, "BEGIN DEFERRED", state=None):
            yield
    finally:
        if context is not None:
            conn.row_factory = original_row_factory
        if stack is not None:
            stack.pop()
        _READ_TXN.depth = 0
        _READ_TXN.conn = None


def read_data_version(conn: sqlite3.Connection) -> int:
    """Capture PRAGMA data_version for optimistic read-then-write consistency."""
    row = _exec(conn, "PRAGMA data_version").fetchone()
    return int(row[0])


def read_txn_depth() -> int:
    """Current read-transaction nesting depth on this thread (public door for tests)."""
    return getattr(_READ_TXN, "depth", 0)


def assert_data_version_stable(conn: sqlite3.Connection, before: int) -> None:
    """Raise ReadRejected if data_version changed since `before` was captured."""
    after = read_data_version(conn)
    if after != before:
        raise ReadRejected(f"data_version changed from {before} to {after}; concurrent write detected")


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
    "ReadRejected",
    "allow_on_loop",
    "allow_on_loop_read",
    "assert_data_version_stable",
    "assert_off_main_thread",
    "in_txn",
    "read_data_version",
    "read_txn_depth",
    "txn_read",
    "txn_write",
    "wal_checkpoint",
]
