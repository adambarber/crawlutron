"""The engine binds no transport cache at module level and takes no in-function transport imports: caches are
registry-owned singletons; a module-level binding, ``global`` rebind, or function-level
``crawlutron.transport`` import reds naming the line.
"""

from __future__ import annotations

import ast

import pytest

from tests.chokepoint_walks import REPO_ROOT
from tests.invariants.test_harness_doubles import node_names

_ENGINE = REPO_ROOT / "crawlutron" / "orchestration" / "engine.py"

#: The transport cache modules whose instances the engine must never own.
_TRANSPORT_CACHE_MODULES = {
    "crawlutron.transport.cache",
    "crawlutron.transport.content_cache",
}


def _imported_from_transport_cache(tree: ast.Module) -> set[str]:
    """Local names bound to a transport cache type by a top-level from-import."""
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in _TRANSPORT_CACHE_MODULES:
            bound.update(alias.asname or alias.name for alias in node.names)
    return bound


def _module_level_cache_bindings(tree: ast.Module) -> list[tuple[str, int]]:
    """Module-level bindings whose RHS constructs or aliases a transport cache.

    Direct ``HttpCache()``/``ContentCache()`` calls and aliases of an imported
    cache class both bind an engine-owned cache instance, whatever the name."""
    cache_names = _imported_from_transport_cache(tree)
    hits: list[tuple[str, int]] = []
    for node in tree.body:
        for name in node_names(node):
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if value is None:
                continue
            constructs = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in cache_names
                for n in ast.walk(value)
            )
            aliases = isinstance(value, ast.Name) and value.id in cache_names
            if constructs or aliases:
                hits.append((name, node.lineno))
    return hits


def _global_rebinds_of_cache_bindings(tree: ast.Module) -> list[tuple[str, int]]:
    """``global`` statements naming a module-bound cache instance (any name)."""
    bound = {name for name, _ in _module_level_cache_bindings(tree)}
    return [
        (name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
        if name in bound
    ]


def _function_level_transport_imports(tree: ast.Module) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            module = (
                stmt.module
                if isinstance(stmt, ast.ImportFrom)
                else ", ".join(alias.name for alias in stmt.names)
            )
            if module and module.startswith("crawlutron.transport"):
                hits.append((module, stmt.lineno))
    return hits


@pytest.mark.parametrize(
    "detector",
    [
        _module_level_cache_bindings,
        _global_rebinds_of_cache_bindings,
        _function_level_transport_imports,
    ],
)
def test_engine_import_seam_is_collapsed(detector) -> None:
    offenders = detector(ast.parse(_ENGINE.read_text(encoding="utf-8")))
    assert not offenders, (
        f"crawlutron/orchestration/engine.py reintroduced the engine-transport "
        f"cache/import seam: {offenders} (name, line) -- shared caches are "
        f"transport-owned singletons; transport imports are top-level"
    )
