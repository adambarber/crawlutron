"""Read-door invariant: every bounded storage read funnels through txn_read.

Receiver rule is conn or ... ._conn (same as write gate); aliased receivers escape;
bare reads outside any door are offenders.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3

from crawlutron.storage.safe_db import read_txn_depth, txn_read
from tests import _walk_doors as _dw
from tests import _walk_primitives as _wp
from tests import _walk_readers as _wr
from tests import chokepoint_walks as _cw
from tests.rules import Rule

_STORAGE_DIR = _wp.REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "storage"

_READ_METHODS = frozenset({"execute", "executemany"})
_READ_KEYWORDS = frozenset({"select", "pragma", "with", "explain"})
_READ_DOOR = frozenset({"txn_read", "txn_write", "in_txn"})

_SCOPE_EXCLUDE = frozenset({"sqlite_store.py"})
READ_DOOR_BUDGET = 16


def _bare_read_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Bare storage reads outside the read door, file and line named."""
    out: list[str] = []
    parents = _wp.parent_map(tree)
    for call, _callee in _wr.attribute_calls(tree):
        callee = _dw.conn_callee(call, _READ_METHODS)
        if callee is None:
            continue
        sql = _dw.sql_operand(call, tree)
        if sql is None:
            continue
        if not (head := sql.lstrip().split()) or head[0].lower() not in _READ_KEYWORDS:
            continue
        owner = _wp.enclosing_function(call, parents)
        configured = owner is not None and owner.name == "configure_store_connection"
        if _dw.inside_door(call, tree, parents, _READ_DOOR) or configured:
            continue
        out.append(_wp.offender_at(path, call, "bare read statement outside the read door"))
    return out


READ_DOOR_RULE = Rule(
    name="storage-reads-funnel-through-the-read-door",
    find=_bare_read_offenders,
    summary=(
        "executes a SELECT/PRAGMA/WITH/EXPLAIN outside txn_read/txn_write/in_txn; "
        "decidable SQL: literal, f-string literal prefix, module-constant name; "
        "parameter/attribute SQL operand skipped, not decided"
    ),
    scope=lambda *, exclude: (
        (path, tree)
        for path, tree in _cw.trees_under(_STORAGE_DIR)
        if path.name not in _SCOPE_EXCLUDE and "migrations" not in path.parts
    ),
    bite=lambda: (
        'conn.execute("SELECT a FROM b")\n',
        'conn.execute("PRAGMA database_list")\n',
        'alias._conn.execute("SELECT a FROM b")\n',
        'SQL = "SELECT a FROM b"\nconn.execute(SQL)\n',
        'conn.execute(f"SELECT {col} FROM b")\n',
    ),
    clean=lambda: (
        'with txn_read(conn):\n    conn.execute("SELECT count(*) FROM t")\n',
        'with txn_write(conn):\n    conn.execute("SELECT 1 FROM t")\n',
        'with in_txn(conn):\n    conn.execute("SELECT 1 FROM t")\n',
        'conn.execute("INSERT INTO t (a, b) VALUES (1, 2)")\n',
        'conn.execute("BEGIN")\n',
        'something._rows.execute("SELECT a FROM b")\n',
    ),
)


def _count_offenders() -> list[str]:
    scope = READ_DOOR_RULE.scope
    if scope is None:
        raise AssertionError("read-door rule has no scope")
    offenders: list[str] = []
    for path, tree in scope(exclude=frozenset()):
        offenders.extend(_bare_read_offenders(tree, path))
    return offenders


def test_storage_reads_funnel_through_the_read_door() -> None:
    offenders = _count_offenders()
    assert len(offenders) <= READ_DOOR_BUDGET, (
        f"{len(offenders)} bare read(s) outside the read door, budget is {READ_DOOR_BUDGET}:\n  "
        + "\n  ".join(offenders)
    )


def test_read_door_budget_is_tight() -> None:
    offenders = _count_offenders()
    assert len(offenders) == READ_DOOR_BUDGET, (
        f"read-door budget is stale: {len(offenders)} offender(s), budget is {READ_DOOR_BUDGET}; "
        "lower it to the measured count:\n  " + "\n  ".join(offenders)
    )


def test_read_door_rule_has_working_bite() -> None:
    READ_DOOR_RULE.check_bites()


# --------------------------------------------------------------------------- reentrancy


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t VALUES (1, 10), (2, 20)")
    conn.commit()
    return conn


def test_txn_read_reentrant_joins_outer_snapshot() -> None:
    """Nested txn_read joins the outer snapshot: one BEGIN, same rows, depth restored."""
    conn = _connect()
    begin_count = 0

    def trace(sql: str) -> None:
        nonlocal begin_count
        if sql.strip().upper().startswith("BEGIN"):
            begin_count += 1

    conn.set_trace_callback(trace)
    depth_values: list[int] = []

    try:
        with txn_read(conn, owner="outer"):
            depth_values.append(read_txn_depth())
            outer_rows = conn.execute("SELECT * FROM t").fetchall()

            with txn_read(conn, owner="inner"):
                depth_values.append(read_txn_depth())
                inner_rows = conn.execute("SELECT * FROM t").fetchall()

            depth_values.append(read_txn_depth())
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert begin_count == 1, f"expected exactly one BEGIN, got {begin_count}"
    assert inner_rows == outer_rows, "inner and outer must see the same snapshot"
    assert depth_values == [1, 2, 1], f"depth should be 1->2->1, got {depth_values}"
    assert read_txn_depth() == 0, "depth restored to zero after outer exits"


def test_txn_read_reentrant_depth_restored_on_exception() -> None:
    """Depth is restored to zero even when the nested read raises."""
    conn = _connect()
    try:
        try:
            with txn_read(conn, owner="outer"):
                with txn_read(conn, owner="inner"):
                    raise ValueError("boom")
        except ValueError:
            pass
    finally:
        conn.close()
    assert read_txn_depth() == 0, "depth restored to zero after exception"


def test_txn_read_reentrant_cannot_escape_outer_snapshot() -> None:
    """A nested read cannot open its own snapshot: no second BEGIN, same rows."""
    conn = _connect()
    begin_sqls: list[str] = []

    def trace(sql: str) -> None:
        if sql.strip().upper().startswith("BEGIN"):
            begin_sqls.append(sql)

    conn.set_trace_callback(trace)
    try:
        with txn_read(conn, owner="outer"):
            before = conn.execute("SELECT * FROM t").fetchall()
            with txn_read(conn, owner="inner"):
                after = conn.execute("SELECT * FROM t").fetchall()
            assert len(begin_sqls) == 1, f"expected exactly one BEGIN, got {len(begin_sqls)}"
            assert before == after, "inner must see the outer snapshot"
    finally:
        conn.set_trace_callback(None)
        conn.close()
