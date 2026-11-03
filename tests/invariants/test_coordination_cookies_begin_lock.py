"""Reads and writes both take the store lock; one transaction at a time.

``load`` and ``slots_for_namespace`` now take ``self._lock`` so no thread can
execute on the shared connection while a writer holds ``BEGIN IMMEDIATE``.
Every read method must be found inside ``with self._lock:``; any read that
touches ``self._conn`` without the lock is an offender.

``merge``/``put``/``touch``/``clear`` still begin inside the lock, so every
``BEGIN IMMEDIATE`` remains inside ``with self._lock:``.
"""

import ast

from tests._walk_assertions import assert_detectors_clean
from tests.chokepoint_walks import REPO_ROOT, offender_at, parent_map, parsed

_STORE = REPO_ROOT / "crawlutron" / "storage" / "coordination_cookies.py"

_READ_METHODS = ("load", "slots_for_namespace")
_WRITE_METHODS = ("merge", "put", "touch", "clear")


def test_reads_take_store_lock_and_begin_stays_inside_lock() -> None:
    tree = parsed(_STORE)
    assert_detectors_clean(
        tree,
        path=_STORE,
        detectors=(
            _read_methods_must_use_store_lock,
            _begin_immediate_outside_lock,
        ),
    )


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


def _is_begin_immediate_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "execute"):
        return False
    return any(
        isinstance(a, ast.Constant) and isinstance(a.value, str) and "BEGIN IMMEDIATE" in a.value
        for a in node.args
    )


def _method_target(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _read_methods_must_use_store_lock(tree: ast.Module) -> list[str]:
    """Flag a read method whose body touches self._conn without self._lock."""
    out: list[str] = []
    for method in _READ_METHODS:
        target = _method_target(tree, method)
        if target is None:
            continue
        has_lock = any(_is_self_lock(node) for node in ast.walk(target))
        has_conn_exec = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr == "_conn"
            for node in ast.walk(target)
        )
        if has_conn_exec and not has_lock:
            out.append(offender_at(_STORE, target, f"{method} touches self._conn without self._lock"))
    return out


def _ancestor_is_store_lock_with(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    anc = parents.get(id(node))
    while anc is not None:
        if _is_store_lock_with(anc):
            return True
        anc = parents.get(id(anc))
    return False


def _begin_immediate_outside_lock(tree: ast.Module) -> list[str]:
    """Every ``BEGIN IMMEDIATE`` must be inside ``with self._lock:`` on the shared
    connection (one transaction at a time)."""
    parents = parent_map(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not _is_begin_immediate_call(node):
            continue
        if _ancestor_is_store_lock_with(node, parents):
            continue
        out.append(
            offender_at(
                _STORE,
                node,
                "BEGIN IMMEDIATE outside self._lock; shared connection needs one transaction at a time",
            )
        )
    return out
