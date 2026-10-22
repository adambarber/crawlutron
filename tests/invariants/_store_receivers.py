"""Where a store-typed receiver comes from: the resolution vocabulary for the
on-the-loop detector. Parameters, local binds, conventional names, ``self.attr``
binds, module-level store globals and one hop of helper indirection resolve here.
"""

from __future__ import annotations

import ast

from tests.chokepoint_walks import assignments, class_defs, functions, nodes_of, offender_at

__all__ = [
    "_CONVENTIONAL_STORE_NAMES",
    "_STORE_CLASSES",
    "_helper_store_bites",
    "_module_store_globals",
    "_named_store",
    "_self_attr_receivers",
    "_store_receivers",
]

# No async frame in app/ may call a SYNCHRONOUS store method directly.
_STORE_CLASSES = (
    "JobStore",
    "CrawlResultStore",
    "WatchStore",
    "CoordinationCounters",
    "SharedCookieStore",
    "CallerKeyStore",
)


# Injected unannotated (FastAPI ``Depends``, lifespan globals, runner args), so a type-only scan sees nothing.
_CONVENTIONAL_STORE_NAMES = {
    "job_store": "JobStore",
    "result_store": "CrawlResultStore",
    "watch_store": "WatchStore",
}


def _named_store(expr: ast.expr | None) -> str | None:
    if not isinstance(expr, (ast.Call, ast.Name)):
        return None
    leaf = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(leaf, ast.Name) and leaf.id in _STORE_CLASSES:
        return leaf.id
    return None


def _self_attr_receivers(tree: ast.AST | None) -> dict[str, str]:
    if tree is None:
        return {}
    receivers: dict[str, str] = {}
    for cls in class_defs(tree):
        for node, target, value in assignments(nodes_of(cls)):
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
            owned = _named_store(value)
            if owned is None and isinstance(node, ast.AnnAssign):
                owned = _named_store(node.annotation)
            if owned is not None:
                receivers[ast.unparse(target)] = owned
    return receivers


def _store_receivers(function: ast.AST, tree: ast.AST | None = None) -> dict[str, str]:
    """Name -> store class, for the store-typed bindings of one function."""
    receivers: dict[str, str] = {}
    for node in nodes_of(function):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotation = ast.unparse(node.annotation)
            if annotation in _STORE_CLASSES:
                receivers[node.arg] = annotation
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and (owned := _named_store(node.value)):
                    receivers[target.id] = owned
    receivers.update(_CONVENTIONAL_STORE_NAMES)
    receivers.update(_self_attr_receivers(tree))
    for _stmt, target, value in assignments(nodes_of(function)):
        if not isinstance(target, ast.Name) or value is None or not isinstance(value, ast.Name):
            continue
        if value.id in receivers:
            receivers[target.id] = receivers[value.id]
    return receivers


def _module_store_globals(tree: ast.AST | None) -> dict[str, str]:
    """Module-level names bound to a store instance: every function sees them."""
    if tree is None:
        return {}
    globals_: dict[str, str] = {}
    for node, target, value in assignments(tree.body):
        if not isinstance(target, ast.Name):
            continue
        annotation = node.annotation if isinstance(node, ast.AnnAssign) else None
        if owned := _named_store(value) or _named_store(annotation):
            globals_[target.id] = owned
    return globals_


def _helper_store_bites(function, tree, sync_methods, path):
    """Sync store calls one hop of same-module helper indirection reaches.

    A sync helper taking a known receiver and calling a store method on it runs
    that call on the caller's loop; one hop only, deeper is a call-graph question.
    """
    if tree is None:
        return []
    helpers = {f.name: f for f in functions(tree) if isinstance(f, ast.FunctionDef)}
    callers = _store_receivers(function, tree)
    offenders: list[str] = []
    for inner in nodes_of(function):
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)):
            continue
        helper = helpers.get(inner.func.id)
        if helper is None or not any(isinstance(a, ast.Name) and a.id in callers for a in inner.args):
            continue
        helper_receivers = _store_receivers(helper, tree)
        offenders.extend(
            offender_at(
                path,
                call,
                f"{ast.unparse(call.func.value)}.{call.func.attr} is synchronous, reached"
                f" through the sync helper {inner.func.id}(); await an async twin there",
            )
            for call in nodes_of(helper)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and (owner := helper_receivers.get(ast.unparse(call.func.value))) is not None
            and call.func.attr in sync_methods[owner]
        )
    return offenders
