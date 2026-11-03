"""``SharedCookieStore.load`` is read-only: a SELECT and a decode, nothing more.

Every read used to be a write transaction (SELECT + UPDATE touched_at + commit),
generating WAL frames and checkpoint pressure on read-heavy workloads. ``load``
must stay a pure read; recency refresh goes through the explicit ``touch``
door. No UPDATE/INSERT/DELETE/COMMIT/ROLLBACK may appear in ``load``'s body.
"""

import ast

from tests.chokepoint_walks import REPO_ROOT, parsed, stmt_offenders

_STORE = REPO_ROOT / "crawlutron" / "storage" / "coordination_cookies.py"


def test_load_is_read_only() -> None:
    tree = parsed(_STORE)
    assert not _load_mutation_offenders(tree), "load performs a mutation"


def _is_load_fn(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef) and node.name == "load"


def _is_mutation_stmt(stmt: ast.AST) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    if isinstance(call.func, ast.Attribute) and call.func.attr == "execute":
        for a in call.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                head = a.value.lstrip().upper()
                if head.startswith(("UPDATE", "INSERT", "DELETE")):
                    return True
    return isinstance(call.func, ast.Attribute) and call.func.attr in {"commit", "rollback"}


def _load_mutation_offenders(tree: ast.Module) -> list[str]:
    return stmt_offenders(tree, _STORE, _is_load_fn, _is_mutation_stmt, "mutation inside load")
