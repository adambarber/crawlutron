"""txn_read conditional counting factory: row_factory untouched unless bounds declared."""

from __future__ import annotations

import sqlite3

import pytest

from crawlutron.storage.safe_db import ReadRejected, txn_read


def _make_conn() -> sqlite3.Connection:
    """Thin database-backed connection with a custom row factory."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES ('a', '1')")
    conn.execute("INSERT INTO t VALUES ('b', '2')")
    conn.execute("COMMIT")
    return conn


def test_unbounded_txn_read_does_not_touch_row_factory() -> None:
    """An unbounded ``txn_read`` leaves ``conn.row_factory`` untouched."""
    conn = _make_conn()
    sentinel = sqlite3.Row
    conn.row_factory = sentinel
    seen: list[bool] = []
    with txn_read(conn):
        seen.append(conn.row_factory is sentinel)
        row = conn.execute("SELECT * FROM t WHERE k = ?", ("a",)).fetchone()
        assert row["v"] == "1"
    # row_factory was NOT replaced — the invariant test.
    assert seen == [True]
    assert conn.row_factory is sentinel


def test_bounded_txn_read_installs_counting_factory() -> None:
    """A bounded ``txn_read`` installs a counting factory that enforces caps."""
    conn = _make_conn()
    original = conn.row_factory
    with pytest.raises(ReadRejected, match="read row cap exceeded"):
        with txn_read(conn, max_rows=1):
            conn.execute("SELECT * FROM t").fetchall()
    assert conn.row_factory is original


def test_bounded_txn_read_max_bytes() -> None:
    """``max_bytes`` caps on the total string-length of returned rows."""
    conn = _make_conn()
    original = conn.row_factory
    with pytest.raises(ReadRejected, match="read size cap exceeded"):
        with txn_read(conn, max_bytes=1):
            conn.execute("SELECT * FROM t").fetchall()
    assert conn.row_factory is original


def test_bounded_txn_read_deadline_expired() -> None:
    """A past ``deadline`` raises immediately before the yield."""
    conn = _make_conn()
    with pytest.raises(ReadRejected, match="read deadline exceeded"):
        with txn_read(conn, deadline=0.0):
            pass


def test_unbounded_txn_read_allows_json_decoding() -> None:
    """An unbounded txn_read does not interfere with the caller's row_factory."""
    conn = _make_conn()
    conn.row_factory = sqlite3.Row
    with txn_read(conn):
        rows = conn.execute("SELECT * FROM t ORDER BY k").fetchall()
    assert len(rows) == 2
    assert conn.row_factory is sqlite3.Row
