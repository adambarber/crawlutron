"""Shared AST-walk helpers for the chokepoint invariant (drift) tests.

Each safety chokepoint -- safe_regex, bounded_io, BoundedTTLCache, SafeUrl,
env parsing -- is guarded by an invariant test that walks app/*.py, parses
every module, and flags a banned pattern. The file walk, the parse loop, the
safe_regex.py exemption, and the offender-label format are the same across
all of them; they live here so the enforcement tests share ONE traversal.
The detectors themselves stay next to the contract they pin (in the test
files); this module only removes the repeated ~10-line walk scaffold per
test that otherwise invites the exemptions and labels to drift apart.

Keeping this pure-stdlib means the static invariant tests do not pay the
heavy ``helpers`` -> engine -> settings import chain.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator
from functools import lru_cache

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
APP_ROOT = REPO_ROOT / "app"
STRATEGIES_DIR = APP_ROOT / "crawl" / "strategies"

# app/safe_regex.py is the sanctioned regex wrapper (stdlib `re` for
# literals; the third-party `regex` engine for bounded execution). It owns
# the banned primitives, so a full-tree walk excludes it. A test that must
# also exempt its own chokepoint module passes `exclude=SANCTIONED | {"..."}`.
SANCTIONED = frozenset({"safe_regex.py"})


def app_python_files(*, exclude: frozenset[str] = SANCTIONED) -> list[pathlib.Path]:
    """Every app/*.py module except the sanctioned chokepoint wrappers."""
    return sorted(p for p in APP_ROOT.rglob("*.py") if p.name not in exclude)


def app_trees(*, exclude: frozenset[str] = SANCTIONED) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """Yield ``(path, tree)`` for the same files ``app_python_files`` walks."""
    for path in app_python_files(exclude=exclude):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def app_offenders(detector, *, exclude: frozenset[str] = SANCTIONED) -> list[str]:
    """Run one app-tree detector and collect its formatted offenders."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=exclude):
        offenders.extend(detector(tree, path))
    return offenders


def calls_named(tree: ast.AST, function_name: str) -> Iterator[ast.Call]:
    """Yield direct calls to one named function in an AST tree."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == function_name
        ):
            yield node


def call_keyword_values(
    tree: ast.AST,
    *,
    function_names: str | frozenset[str],
    keyword_names: str | tuple[str, ...],
) -> Iterator[tuple[ast.Call, ast.expr]]:
    """Yield selected keyword values from direct calls to named functions."""
    names = {function_names} if isinstance(function_names, str) else function_names
    keywords = {keyword_names} if isinstance(keyword_names, str) else set(keyword_names)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in names
        ):
            continue
        # One pair per DISTINCT keyword name: Python itself rejects a repeated
        # keyword, so a call carrying two is un-compilable source, and emitting
        # an offender per occurrence would over-report against the one-per-call
        # behavior these detectors had before they were unified.
        seen_keywords: set[str | None] = set()
        for keyword in node.keywords:
            if keyword.arg in keywords and keyword.arg not in seen_keywords:
                seen_keywords.add(keyword.arg)
                yield node, keyword.value


def trees_under(directory: pathlib.Path) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """Yield ``(path, tree)`` for every module under ``directory``."""
    for path in sorted(directory.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def file_tree(relative: str) -> tuple[pathlib.Path, ast.Module]:
    """Parse one module by its repo-relative path (e.g. app/api/webhooks.py)."""
    path = REPO_ROOT / relative
    return path, ast.parse(path.read_text(encoding="utf-8"))


def offender(path: pathlib.Path, lineno: int, detail: str = "") -> str:
    """Format an offender as ``app/pkg/mod.py:lineno detail`` for messages."""
    label = f"{path.relative_to(REPO_ROOT)}:{lineno}"
    return f"{label} {detail}".strip()


def _static_string(expr: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _static_string(expr.left, bindings)
        right = _static_string(expr.right, bindings)
        return left + right if left is not None and right is not None else None
    if isinstance(expr, ast.JoinedStr):
        parts = [_static_string(value, bindings) for value in expr.values]
        if all(part is not None for part in parts):
            return "".join(parts)
    return None


@lru_cache(maxsize=256)
def _import_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                bindings[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                bindings[local] = alias.name if alias.asname else local
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            canonical = "" if isinstance(value, ast.Call) else _resolved_expr(value, bindings)
            canonical = canonical or _static_string(value, bindings)
            if not canonical:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in bindings:
                    bindings[target.id] = canonical
                    changed = True
    return bindings


def _resolved_expr(expr: ast.AST, bindings: dict[str, str]) -> str:
    """Resolve import aliases and constant-folded ``getattr`` names.

    Only literals, concatenations, and constant f-strings are folded for an
    attribute name. Runtime-computed values remain opaque by design.
    """
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolved_expr(expr.value, bindings)
        return f"{base}.{expr.attr}" if base else expr.attr
    if isinstance(expr, ast.Call):
        callee = _resolved_expr(expr.func, bindings)
        if callee.rsplit(".", 1)[-1] == "getattr" and len(expr.args) >= 2:
            attribute = _static_string(expr.args[1], bindings)
            base = _resolved_expr(expr.args[0], bindings)
            if attribute is not None and base:
                return f"{base}.{attribute}"
        return callee
    return ""


def resolved_call_name(tree: ast.AST, call: ast.Call) -> str:
    """Return a call's import-resolved dotted name for AST detectors."""
    return _resolved_expr(call, _import_bindings(tree))
