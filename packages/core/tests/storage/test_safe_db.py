"""Safe read/write/update/destroy primitives on one shared SQLite connection."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import cast

import pytest

from crawlutron.storage.coordination_throttle_store import CoordinationThrottleStore
from crawlutron.storage.safe_db import (
    ReadRejected,
    StorageBusy,
    StoreClosed,
    StoreConnectionPoisoned,
    WriteRejected,
    allow_on_loop,
    allow_on_loop_read,
    assert_data_version_stable,
    in_txn,
    read_data_version,
    read_txn_depth,
    txn_read,
    txn_write,
    wal_checkpoint,
)


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER)")
    return conn


class _FailingConn:
    """A connection whose every statement fails busy."""

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        raise sqlite3.OperationalError("database is locked")


class _CommitFailsConn:
    """A connection that begins fine but fails COMMIT."""

    def __init__(self) -> None:
        self.began = False

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        if sql.strip().upper().startswith("BEGIN"):
            self.began = True
            return cast(sqlite3.Cursor, None)
        raise sqlite3.Error("commit failed")


def test_txn_write_commits_a_write(tmp_path) -> None:
    db_path = tmp_path / "commit.db"
    conn = _connect(db_path)
    try:
        with txn_write(conn, owner="test.m"):
            conn.execute("INSERT INTO t (k, v) VALUES (1, 2)")
        row = conn.execute("SELECT v FROM t WHERE k = 1").fetchone()
        assert row is not None and row[0] == 2
    finally:
        conn.close()


def test_txn_write_rolls_back_on_exception(tmp_path) -> None:
    db_path = tmp_path / "rollback.db"
    conn = _connect(db_path)
    try:
        with pytest.raises(ValueError, match="boom"):
            with txn_write(conn, owner="test.m"):
                conn.execute("INSERT INTO t (k, v) VALUES (1, 2)")
                raise ValueError("boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_txn_write_rejects_nesting(tmp_path) -> None:
    db_path = tmp_path / "nest.db"
    conn = _connect(db_path)
    try:
        with txn_write(conn, owner="a"):
            with pytest.raises(WriteRejected, match="nested"):
                with txn_write(conn, owner="b"):
                    pass
    finally:
        conn.close()


def test_txn_write_rejects_second_concurrent_txn_same_thread(tmp_path) -> None:
    conn1 = _connect(tmp_path / "one.db")
    conn2 = _connect(tmp_path / "two.db")
    try:
        with txn_write(conn1, owner="a"):
            with pytest.raises(WriteRejected, match="second concurrent"):
                with txn_write(conn2, owner="b"):
                    pass
    finally:
        conn1.close()
        conn2.close()


def test_in_txn_requires_active_transaction() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        with pytest.raises(WriteRejected, match="outside"):
            with in_txn(conn):
                pass
    finally:
        conn.close()


def test_in_txn_passes_inside_txn_write(tmp_path) -> None:
    db_path = tmp_path / "in_txn.db"
    conn = _connect(db_path)
    try:
        with txn_write(conn, owner="test.m"):
            with in_txn(conn):
                conn.execute("INSERT INTO t (k, v) VALUES (3, 4)")
        assert conn.execute("SELECT v FROM t WHERE k = 3").fetchone()[0] == 4
    finally:
        conn.close()


def test_write_rejected_on_loop_thread() -> None:
    async def main() -> None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            with pytest.raises(WriteRejected, match="event-loop thread"):
                with txn_write(conn, owner="m"):
                    pass
        finally:
            conn.close()

    asyncio.run(main())


def test_allow_on_loop_permits_loop_thread_write() -> None:
    async def main() -> None:
        conn = sqlite3.connect(":memory:")
        try:
            with allow_on_loop():
                with txn_write(conn, owner="m"):
                    conn.execute("CREATE TABLE u (k INTEGER)")
        finally:
            conn.close()

    asyncio.run(main())


def test_txn_write_raises_storage_busy_on_begin_failure() -> None:
    with pytest.raises(StorageBusy):
        with txn_write(cast(sqlite3.Connection, _FailingConn()), owner="m"):
            pass


def test_txn_write_raises_store_connection_poisoned_on_commit_failure() -> None:
    failing = _CommitFailsConn()
    conn = cast(sqlite3.Connection, failing)
    with pytest.raises(StoreConnectionPoisoned):
        with txn_write(conn, owner="m"):
            assert failing.began
    assert failing.began


def test_txn_write_raises_store_closed_on_closed_connection() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.close()
    with pytest.raises(StoreClosed):
        with txn_write(conn, owner="m"):
            pass


def test_txn_read_commits_a_read(tmp_path) -> None:
    db_path = tmp_path / "read.db"
    conn = _connect(db_path)
    conn.execute("INSERT INTO t (k, v) VALUES (5, 6)")
    try:
        with txn_read(conn):
            row = conn.execute("SELECT v FROM t WHERE k = 5").fetchone()
        assert row is not None and row[0] == 6
    finally:
        conn.close()


def test_wal_checkpoint_runs_without_error(tmp_path) -> None:
    db_path = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("CREATE TABLE t (k INTEGER)")
        wal_checkpoint(conn)
    finally:
        conn.close()


def test_check_then_failure_no_longer_wedges_the_connection(tmp_path) -> None:
    """C1 regression: check_key_bucket then record_key_failure must not wedge the shared connection."""
    store = CoordinationThrottleStore(tmp_path / "c1.db")
    try:
        store.check_key_bucket("key-1", now=10.0)
        store.record_key_failure(
            "key-1",
            now=10.0,
            max_failures=3,
            failure_window_seconds=60.0,
            cooldown_seconds=30.0,
            bucket_capacity=1024,
        )
        store.check_key_bucket("key-1", now=11.0)
    finally:
        store.close()


def test_read_rejected_on_loop_thread() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:

        async def _run() -> None:
            with pytest.raises(ReadRejected, match="event-loop thread"):
                with txn_read(conn):
                    pass

        asyncio.run(_run())
    finally:
        conn.close()


def test_allow_on_loop_read_permits_loop_thread_read() -> None:
    async def main() -> None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER)")
        try:
            with allow_on_loop_read():
                with txn_read(conn):
                    conn.execute("INSERT INTO t (k, v) VALUES (1, 2)")
        finally:
            conn.close()

    asyncio.run(main())


def test_read_rejects_over_max_rows(tmp_path) -> None:
    db_path = tmp_path / "rows.db"
    conn = _connect(db_path)
    for i in range(10):
        conn.execute("INSERT INTO t (k, v) VALUES (?, ?)", (i, i))
    try:
        with pytest.raises(ReadRejected, match="row cap"):
            with txn_read(conn, max_rows=5):
                conn.execute("SELECT * FROM t").fetchall()
    finally:
        conn.close()


def test_read_rejects_over_max_bytes(tmp_path) -> None:
    db_path = tmp_path / "bytes.db"
    conn = _connect(db_path)
    conn.execute("INSERT INTO t (k, v) VALUES (1, ?)", ("x" * 1000,))
    try:
        with pytest.raises(ReadRejected, match="size cap"):
            with txn_read(conn, max_bytes=100):
                conn.execute("SELECT * FROM t").fetchall()
    finally:
        conn.close()


def test_read_rejects_deadline_exceeded(tmp_path) -> None:
    db_path = tmp_path / "deadline.db"
    conn = _connect(db_path)
    for i in range(100):
        conn.execute("INSERT INTO t (k, v) VALUES (?, ?)", (i, i))
    try:
        past = time.monotonic() - 1.0
        with pytest.raises(ReadRejected, match="deadline"):
            with txn_read(conn, deadline=past):
                conn.execute("SELECT * FROM t").fetchall()
    finally:
        conn.close()


def test_txn_read_commits_and_rolls_back(tmp_path) -> None:
    db_path = tmp_path / "read_txn.db"
    conn = _connect(db_path)
    conn.execute("INSERT INTO t (k, v) VALUES (9, 2)")
    try:
        with txn_read(conn, owner="read.m"):
            row = conn.execute("SELECT v FROM t WHERE k = 9").fetchone()
        assert row is not None and row[0] == 2
    finally:
        conn.close()

    db_path2 = tmp_path / "read_txn_rollback.db"
    conn2 = _connect(db_path2)
    conn2.execute("INSERT INTO t (k, v) VALUES (3, 4)")
    try:
        with pytest.raises(ValueError, match="boom"):
            with txn_read(conn2, owner="read.m"):
                conn2.execute("INSERT INTO t (k, v) VALUES (5, 6)")
                raise ValueError("boom")
        assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn2.close()


def test_txn_read_reentrant_joins_same_connection(tmp_path) -> None:
    """A nested txn_read on the same thread+connection joins the outer snapshot."""
    db_path = tmp_path / "read_reentrant.db"
    conn = _connect(db_path)
    try:
        depths: list[int] = []

        def trace(sql: str) -> None:
            pass  # BEGIN counting is not needed; depth tells the story.

        conn.set_trace_callback(trace)
        try:
            with txn_read(conn, owner="outer"):
                depths.append(read_txn_depth())
                with txn_read(conn, owner="inner"):
                    depths.append(read_txn_depth())
                depths.append(read_txn_depth())
            assert depths == [1, 2, 1], f"depth should be 1->2->1, got {depths}"
        finally:
            conn.set_trace_callback(None)
    finally:
        conn.close()


def test_txn_read_rejects_second_concurrent_txn_same_thread(tmp_path) -> None:
    conn1 = _connect(tmp_path / "read_one.db")
    conn2 = _connect(tmp_path / "read_two.db")
    try:
        with txn_read(conn1, owner="a"):
            with pytest.raises(ReadRejected, match="second concurrent"):
                with txn_read(conn2, owner="b"):
                    pass
    finally:
        conn1.close()
        conn2.close()


def test_read_data_version_detects_concurrent_write(tmp_path) -> None:
    db_path = tmp_path / "version.db"
    conn = _connect(db_path)
    conn.execute("INSERT INTO t (k, v) VALUES (1, 2)")
    try:
        before = read_data_version(conn)
        other = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            other.execute("UPDATE t SET v = 99 WHERE k = 1")
        finally:
            other.close()
        with pytest.raises(ReadRejected, match="data_version changed"):
            assert_data_version_stable(conn, before)
    finally:
        conn.close()
