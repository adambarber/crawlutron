"""Read-door invariant: every bounded storage read funnels through txn_read.

Receiver rule is conn or ... ._conn (same as write gate);
aliased receivers escape; bare reads outside any door are offenders.
"""

from __future__ import annotations

import ast
import pathlib

from tests import _walk_primitives as _wp
from tests import _walk_readers as _wr
from tests import _walk_resolution as _wres
from tests import chokepoint_walks as _cw
from tests.rules import Rule

_REPO_ROOT = _wp.REPO_ROOT
_enclosing_function = _wp.enclosing_function
_offender_at = _wp.offender_at
_parent_map = _wp.parent_map
_attribute_calls = _wr.attribute_calls
_resolved_call_name = _wres.resolved_call_name
_trees_under = _cw.trees_under

_STORAGE_DIR = _REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "storage"

_READ_METHODS = frozenset({"execute", "executemany"})
_READ_KEYWORDS = frozenset({"select", "pragma", "with", "explain"})
_READ_DOOR = frozenset({"txn_read", "txn_write", "in_txn"})

_SCOPE_EXCLUDE = frozenset({"sqlite_store.py"})
READ_DOOR_BUDGET = 41


def _is_conn(node: ast.AST) -> bool:
    """True when node is conn or any ... ._conn chain."""
    return (
        isinstance(node, ast.Name)
        and node.id == "conn"
        or isinstance(node, ast.Attribute)
        and node.attr == "_conn"
    )


def _read_call(call: ast.Call) -> ast.Attribute | None:
    """The callee when call runs execute/executemany on conn or ... ._conn."""
    func = call.func
    return (
        func
        if isinstance(func, ast.Attribute) and func.attr in _READ_METHODS and _is_conn(func.value)
        else None
    )


def _module_sql_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = \"sql\"`` bindings, resolved once per tree."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    bindings[target.id] = node.value.value
    return bindings


def _read_sql(
    call: ast.Call,
    tree: ast.Module | None = None,
    constants: dict[str, str] | None = None,
) -> str | None:
    """The decidable SQL string of call's first argument, or None when opaque."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        literal = "".join(
            v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        return literal or None
    if not isinstance(arg, ast.Name) or tree is None:
        return None
    if constants is None:
        constants = _module_sql_constants(tree)
    return constants.get(arg.id)


def _is_read(sql: str) -> bool:
    return bool(sql.lstrip().split()) and sql.lstrip().split()[0].lower() in _READ_KEYWORDS


def _door_with_blocks(tree: ast.Module) -> list[tuple[int, int, str]]:
    """Line ranges and resolved names of every ``with`` block in the tree."""
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    name = _resolved_call_name(tree, ctx).rsplit(".", 1)[-1]
                    spans.append((node.lineno, node.end_lineno or node.lineno, name))
    return spans


def _inside_door(call: ast.Call, tree: ast.Module, parents: dict[int, ast.AST]) -> bool:
    """True when call sits lexically inside a with txn_read/txn_write/in_txn block."""
    line = call.lineno
    for start, end, name in _door_with_blocks(tree):
        if name in _READ_DOOR and start <= line <= end:
            return True
    return False


def _bare_read_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Bare storage reads outside the read door, file and line named."""
    constants = _module_sql_constants(tree)
    parents = _parent_map(tree)
    out: list[str] = []
    for call, _callee in _attribute_calls(tree):
        callee = _read_call(call)
        if callee is None:
            continue
        sql = _read_sql(call, tree, constants)
        if sql is None or not _is_read(sql):
            continue
        owner = _enclosing_function(call, parents)
        is_configured = owner is not None and owner.name == "configure_store_connection"
        in_door = _inside_door(call, tree, parents)
        if in_door or is_configured:
            continue
        out.append(_offender_at(path, call, "bare read statement outside the read door"))
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
        for path, tree in _trees_under(_STORAGE_DIR)
        if path.name not in _SCOPE_EXCLUDE and "migrations" not in path.parts
    ),
    bite=lambda: (
        'conn.execute("SELECT k, v FROM t")\n',
        'conn.execute("PRAGMA data_version")\n',
        'alias._conn.execute("SELECT k, v FROM t")\n',
        'SQL = "SELECT k, v FROM t"\nconn.execute(SQL)\n',
        'conn.execute(f"SELECT k, v FROM {col}")\n',
    ),
    clean=lambda: (
        'with txn_read(conn):\n    conn.execute("SELECT k, v FROM t")\n',
        'with txn_write(conn):\n    conn.execute("SELECT count(*) FROM t")\n',
        'with in_txn(conn):\n    conn.execute("SELECT 1 FROM t")\n',
        'conn.execute("INSERT INTO t (k, v) VALUES (1, 2)")\n',
        'conn.execute("BEGIN")\n',
        'something._rows.execute("SELECT k FROM t")\n',
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
