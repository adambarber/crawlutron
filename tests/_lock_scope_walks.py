"""Lock-scope detectors: no slow work lexically inside ``with self._lock:``.

Both cache and job-store waves assert the same shape -- a named call must sit
outside the lock block -- so one detector serves both instead of two copies.
"""

import ast
import pathlib

from tests._walk_primitives import REPO_ROOT, parsed


def calls_inside_self_lock(
    path: str, function_names: tuple[str, ...], call_names: tuple[str, ...]
) -> list[str]:
    """Calls named in ``call_names`` under ``with self._lock:`` in the named functions.

    Paths are repo-relative; the file is parsed from disk so the assertion
    reads the live source rather than a stale import."""
    file_path = REPO_ROOT / path
    tree: ast.Module = parsed(file_path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in function_names:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.With):
                continue
            if not any(
                isinstance(item.context_expr, ast.Attribute) and item.context_expr.attr == "_lock"
                for item in child.items
            ):
                continue
            for call in ast.walk(child):
                if not isinstance(call, ast.Call):
                    continue
                name = (
                    call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else call.func.id
                    if isinstance(call.func, ast.Name)
                    else None
                )
                if name in call_names:
                    offenders.append(f"{path}:{call.lineno}: {name}() runs under self._lock")
    return offenders


def public_path(relative: str) -> pathlib.Path:
    return REPO_ROOT / relative
