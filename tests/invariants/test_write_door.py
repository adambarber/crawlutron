"""Write-door invariant: every storage write funnels through txn_write/in_txn.

Decidable SQL: literal, f-string literal prefix, module-constant name; parameter/attribute SQL skipped.
"""

from __future__ import annotations

import ast
import pathlib

from tests._walk_doors import conn_callee, inside_door, sql_operand
from tests._walk_primitives import REPO_ROOT, enclosing_function, offender_at, parent_map
from tests._walk_readers import attribute_calls
from tests.chokepoint_walks import trees_under
from tests.rules import Rule

_STORAGE_DIR = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "storage"

_WRITE_METHODS = frozenset({"execute", "executemany", "commit", "rollback", "executescript"})
_WRITE_KEYWORDS = frozenset({"insert", "update", "delete", "begin"})
_WRITE_DOOR = frozenset({"txn_write", "in_txn"})

_SCOPE_EXCLUDE = frozenset({"sqlite_store.py"})


def _inside_configure_store_connection(call: ast.Call, parents: dict[int, ast.AST]) -> bool:
    owner = enclosing_function(call, parents)
    return owner is not None and owner.name == "configure_store_connection"


def _bare_outside(call: ast.Call, tree: ast.Module, parents: dict[int, ast.AST]) -> bool:
    """True when the call is outside every write door and not in configure_store_connection."""
    return not inside_door(call, tree, parents, _WRITE_DOOR) and not _inside_configure_store_connection(
        call, parents
    )


def _bare_write_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Bare storage writes outside the write door, file and line named."""
    out: list[str] = []
    parents = parent_map(tree)
    for call, _callee in attribute_calls(tree):
        func = conn_callee(call, _WRITE_METHODS)
        if func is None:
            continue
        if func.attr in {"commit", "rollback", "executescript"}:
            if _bare_outside(call, tree, parents):
                out.append(offender_at(path, call, f"direct {func.attr}() outside the write door"))
            continue
        sql = sql_operand(call, tree)
        if sql is None:
            continue
        head = sql.lstrip().split()
        if not head or head[0].lower() not in _WRITE_KEYWORDS:
            continue
        if _bare_outside(call, tree, parents):
            out.append(offender_at(path, call, "bare write statement outside the write door"))
    return out


WRITE_DOOR_RULE = Rule(
    name="storage-writes-funnel-through-the-write-door",
    find=_bare_write_offenders,
    summary=(
        "executes an INSERT/UPDATE/DELETE/BEGIN, or a direct commit/rollback/"
        "executescript, outside txn_write/in_txn/configure_store_connection; "
        "decidable SQL: literal, f-string literal prefix, module-constant name; "
        "parameter/attribute SQL operand skipped, not decided"
    ),
    scope=lambda *, exclude: (
        (path, tree)
        for path, tree in trees_under(_STORAGE_DIR)
        if "migrations" not in path.parts and path.name not in _SCOPE_EXCLUDE
    ),
    bite=lambda: (
        'conn.execute("UPDATE t SET x = 1")\n',
        'alias._conn.execute("UPDATE t SET x = 1")\n',
        'SQL = "UPDATE t SET x = 1"\nconn.execute(SQL)\n',
        'store._conn.execute("BEGIN")\n',
        'conn.execute(f"UPDATE t SET {col} = 1")\n',
        'with txn_write(conn):\n    def helper():\n        conn.execute("UPDATE t SET x = 1")\n',
    ),
    clean=lambda: (
        'with txn_write(conn):\n    conn.execute("UPDATE t SET x = 1")\n',
        'with in_txn(conn):\n    conn.execute("UPDATE t SET x = 1")\n',
        'def configure_store_connection(conn):\n    conn.execute("PRAGMA busy_timeout=5000")\n',
        'def helper():\n    with txn_write(conn):\n        conn.execute("UPDATE t SET x = 1")\n',
        'conn.execute("SELECT * FROM t")\n',
        'self._conn.execute("PRAGMA busy_timeout=5000")\n',
        'something._rows.execute("UPDATE t SET x = 1")\n',
        'conn.execute(f"SELECT * FROM {table}")\n',
    ),
)


def test_storage_writes_funnel_through_the_write_door() -> None:
    scope = WRITE_DOOR_RULE.scope
    if scope is None:
        raise AssertionError("write-door rule has no scope")
    offenders: list[str] = []
    for path, tree in scope(exclude=frozenset()):
        offenders.extend(_bare_write_offenders(tree, path))
    assert not offenders, "\n".join(offenders)


def test_write_door_rule_has_working_bite() -> None:
    WRITE_DOOR_RULE.check_bites()
