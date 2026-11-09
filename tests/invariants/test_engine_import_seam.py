"""The engine holds no module-level shared-cache globals and no in-function
transport imports (external architecture review, #684 family).

The engine's shared caches are registry-owned transport singletons handed down
through the pipeline; a module-level ``_SHARED_*`` binding (or a ``global``
rebind of one) or a function-level ``from crawlutron.transport... import``
reintroduces the seam the collapse removed, so each reds naming the line.
"""

from __future__ import annotations

import ast

import pytest

from tests.chokepoint_walks import REPO_ROOT
from tests.invariants.test_harness_doubles import node_names

_ENGINE = REPO_ROOT / "crawlutron" / "orchestration" / "engine.py"

#: Module-level bindings the engine must never own again (the cache seam).
_FORBIDDEN_GLOBAL_PREFIX = "_SHARED_"


def _forbidden(name: str) -> bool:
    return name.startswith(_FORBIDDEN_GLOBAL_PREFIX)


def _module_level_cache_globals(tree: ast.Module) -> list[tuple[str, int]]:
    return [(name, node.lineno) for node in tree.body for name in node_names(node) if _forbidden(name)]


def _global_rebinds_in_functions(tree: ast.Module) -> list[tuple[str, int]]:
    return [
        (name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
        if _forbidden(name)
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
    [_module_level_cache_globals, _global_rebinds_in_functions, _function_level_transport_imports],
)
def test_engine_import_seam_is_collapsed(detector) -> None:
    offenders = detector(ast.parse(_ENGINE.read_text(encoding="utf-8")))
    assert not offenders, (
        f"crawlutron/orchestration/engine.py reintroduced the engine-transport "
        f"cache/import seam: {offenders} (name, line) -- shared caches are "
        f"transport-owned singletons; transport imports are top-level"
    )
