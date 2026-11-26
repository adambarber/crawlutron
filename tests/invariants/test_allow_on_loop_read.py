"""Read-door gate: `allow_on_loop_read()` may only be called in its own test.

The prose ban failed three times in one arc; this gate makes it mechanical.
"""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import (
    SCAN_ROOTS,
    canonical_tree,
    enclosing_function,
    offender_at,
    parent_map,
    trees_under,
)
from tests.chokepoint_paths import REPO_ROOT
from tests.rules import Registry, Rule

#: The one sanctioned caller: (file, enclosing function name) — never a line number.
_EXCLUDED = "packages/core/tests/storage/test_safe_db.py"
_EXCLUDED_FUNCTION = "test_allow_on_loop_read_permits_loop_thread_read"

#: Lookalike: `allow_on_loop()` is the WRITE facility, a different identifier.


def _offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every `allow_on_loop_read()` call except the sanctioned (file, function)."""
    out: list[str] = []
    parents = parent_map(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if callee_name != "allow_on_loop_read":
            continue
        if path.relative_to(REPO_ROOT).as_posix() == _EXCLUDED:
            owner = enclosing_function(node, parents)
            while owner is not None and owner.name != _EXCLUDED_FUNCTION:
                owner = enclosing_function(owner, parents)
            if owner is not None:
                continue
        out.append(offender_at(path, func, "allow_on_loop_read() called outside its sanctioned test"))
    return out


ALLOW_ON_LOOP_READ_RULE = Rule(
    name="allow-on-loop-read-called-in-its-sanctioned-test-only",
    find=_offenders,
    summary=(
        "allow_on_loop_read() is the read facility; the only live caller is its "
        "own test at packages/core/tests/storage/test_safe_db.py"
    ),
    scope=lambda *, exclude: (
        (path, tree) for root in SCAN_ROOTS for path, tree in trees_under(REPO_ROOT / root)
    ),
    bite=lambda: ("allow_on_loop_read()\n", "safe_db.allow_on_loop_read()\n"),
    clean=lambda: (
        "allow_on_loop()\n",
        "def allow_on_loop_read():\n    pass\n",
        '__all__ = ["allow_on_loop_read"]\n',
    ),
)


RULES = Registry((ALLOW_ON_LOOP_READ_RULE,))


def test_allow_on_loop_read_rule_holds_on_live_tree() -> None:
    RULES.check_all_clean()


def test_allow_on_loop_read_rule_has_working_bite() -> None:
    RULES.check_all_bite()


def test_allow_on_loop_read_walk_past_names_two_offenders() -> None:
    """Two planted calls in scratch modules are both named file:line."""
    source = "def f():\n    allow_on_loop_read()\n"
    trees = (
        (REPO_ROOT / "scratch" / "a.py", ast.parse(source)),
        (REPO_ROOT / "scratch" / "b.py", ast.parse(source)),
    )
    found = ALLOW_ON_LOOP_READ_RULE.violations(trees)
    assert len(found) == 2, found
    assert any("scratch/a.py:2" in o for o in found), found
    assert any("scratch/b.py:2" in o for o in found), found


def test_allow_on_loop_read_exemption_is_live() -> None:
    """The sanctioned function still exists and still calls the facility."""
    path = REPO_ROOT / _EXCLUDED
    tree = canonical_tree(path)
    found_function = False
    found_call = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _EXCLUDED_FUNCTION:
            found_function = True
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    callee = call.func
                    if isinstance(callee, ast.Name) and callee.id == "allow_on_loop_read":
                        found_call = True
    assert found_function, f"{_EXCLUDED_FUNCTION} not found in {_EXCLUDED}"
    assert found_call, f"{_EXCLUDED_FUNCTION} no longer calls allow_on_loop_read()"
