"""No ``BEGIN IMMEDIATE`` may be issued while the process store lock is held.

Holding ``self._lock`` (the one RLock per db file, shared by every store on it)
across a cross-process SQLite write-lock wait pins every in-process thread out
of the store for up to ``busy_timeout`` (5 s). ``merge``/``put``/``touch``/
``clear`` now take the SQLite write lock first, then the store lock -- one
order everywhere -- so no call site may begin a transaction inside
``with self._lock:``.
"""

import ast

from tests.chokepoint_walks import REPO_ROOT, parsed, stmt_offenders

_STORE = REPO_ROOT / "crawlutron" / "storage" / "coordination_cookies.py"


def test_no_begin_immediate_inside_store_lock() -> None:
    tree = parsed(_STORE)
    assert not _begin_immediate_inside_store_lock(tree), "BEGIN IMMEDIATE issued while self._lock is held"


def _is_self_lock(expr: ast.AST) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "self"
        and expr.attr == "_lock"
    )


def _is_store_lock_with(node: ast.AST) -> bool:
    if not isinstance(node, ast.With):
        return False
    return any(_is_self_lock(item.context_expr) for item in node.items)


def _is_begin_immediate(stmt: ast.AST) -> bool:
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    call = stmt.value
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "execute"):
        return False
    return any(
        isinstance(a, ast.Constant) and isinstance(a.value, str) and "BEGIN IMMEDIATE" in a.value
        for a in call.args
    )


def _begin_immediate_inside_store_lock(tree: ast.Module) -> list[str]:
    return stmt_offenders(
        tree, _STORE, _is_store_lock_with, _is_begin_immediate, "BEGIN IMMEDIATE inside store lock"
    )
