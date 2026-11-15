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


def test_txn_read_counts_blob_by_true_byte_length() -> None:
    """A blob is scored by its carried byte length, not its repr length."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v BLOB)")
    conn.execute("INSERT INTO t VALUES (1, ?)", (b"\x00\x01\x02",))
    conn.execute("COMMIT")
    with txn_read(conn, max_bytes=3):
        rows = conn.execute("SELECT v FROM t").fetchall()
        assert len(rows) == 1
    with pytest.raises(ReadRejected, match="size cap"):
        with txn_read(conn, max_bytes=2):
            conn.execute("SELECT v FROM t").fetchall()


def test_txn_read_scores_none_as_zero_bytes() -> None:
    """None carries no bytes; str(None) is four characters and must not count."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t VALUES (1, NULL)")
    conn.execute("COMMIT")
    with txn_read(conn, max_bytes=0):
        conn.execute("SELECT v FROM t").fetchall()


def test_nested_bounded_read_honours_inner_cap() -> None:
    """A nested bounded read enforces its own cap: tightest active bound wins."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)")
    conn.execute("COMMIT")
    with pytest.raises(ReadRejected, match="row cap exceeded"):
        with txn_read(conn, max_rows=10):
            with txn_read(conn, max_rows=1):
                conn.execute("SELECT * FROM t").fetchall()


def test_bounded_txn_read_delegates_to_row_factory() -> None:
    """A bounded read hands rows to the connection's row_factory so name access works."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES ('a', 'one')")
    conn.execute("COMMIT")
    conn.row_factory = sqlite3.Row
    with txn_read(conn, max_rows=10):
        row = conn.execute("SELECT v FROM t WHERE k = 'a'").fetchone()
        assert row is not None and row["v"] == "one"
    assert conn.row_factory is sqlite3.Row
