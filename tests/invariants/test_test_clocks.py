"""The test suite reads ONE clock: tests/_clocks.py. The clock family is matched BY NAME SHAPE, never a hand
list: a new clock-like symbol in tests/** is governed the moment its name matches a family pattern.
"""

from __future__ import annotations

import ast
import fnmatch
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import APP_ROOT, REPO_ROOT, offender_at
from tests.invariants._clock_family import (
    CLOCK_HOME,
    WALL_MEMBERS,
    from_imports,
    from_time_imports,
    suite_trees,
    wall_read,
)
from tests.rules import Bites, Rule, Silent

_CLOCK_NAME_PATTERNS = ("*clock*", "frozen_*", "*_timeline*")

_TEST_DEF_PREFIXES = ("test_", "Test")


def _is_test_named(name: str) -> bool:
    """A collected test/class name: prose about a clock, not a clock def."""
    return name.startswith(_TEST_DEF_PREFIXES)


def _clock_like(name: str) -> bool:
    """Whether a symbol's NAME puts it in the clock family (case-insensitive:
    ``_Clock`` and ``getClock`` are timelines too)."""
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in _CLOCK_NAME_PATTERNS)


def _defined_names(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Every MODULE-LEVEL def/class and binding, with its node (the surface
    convention: methods and inline arrange closures are not symbols)."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node


def _imports_clocks(tree: ast.Module) -> set[str]:
    """Bare names the module binds from ``tests._clocks`` (its re-exports)."""
    return from_imports(tree, frozenset({"tests._clocks", "._clocks", "_clocks"}))


def _in_tests(path: pathlib.Path) -> bool:
    """Whether the sweep's path gate owns this module (tests/** only)."""
    return (REPO_ROOT / "tests") in path.parents or path.parent == REPO_ROOT / "tests"


def _local_clock_offenders(tree: ast.Module, path: pathlib.Path) -> Iterator[str]:
    """Every clock-like symbol DEFINED in a tests module that is not defined
    in or re-exported from ``tests/_clocks.py``."""
    if path == CLOCK_HOME or path.parent.name == "invariants" or not _in_tests(path):
        return
    imported = _imports_clocks(tree)
    for name, node in _defined_names(tree):
        if _clock_like(name) and not _is_test_named(name) and name not in imported:
            yield offender_at(
                path,
                node,
                f"{name} defines its own clock: every clock-like symbol in the suite is "
                f"defined in or re-exported from tests/_clocks.py (#843)",
            )


def _clock_read(node: ast.expr) -> bool:
    """Whether an expression reads a wall clock: ``time.<member>(...)`` or a
    bare from-imported clock member (the ``_FAKE = time.time`` shape)."""
    return wall_read(node, _FROM_IMPORTED)


_FROM_IMPORTED: set[str] = set()


def _bound_clock_offenders(tree: ast.Module, path: pathlib.Path) -> Iterator[str]:
    """A MODULE-LEVEL binding whose value reads a clock directly (``_FAKE =
    time.time``) is a local clock whatever its name spells."""
    if path == CLOCK_HOME or path.parent.name == "invariants" or not _in_tests(path):
        return
    _FROM_IMPORTED.update(from_time_imports(tree, WALL_MEMBERS))
    for name, node in _defined_names(tree):
        if isinstance(node, ast.Assign) and _clock_read(node.value):
            yield offender_at(
                path,
                node,
                f"module-level {name} binds a "
                f"clock read: clocks are constructed through tests/_clocks.py (#843)",
            )


TESTS_DEFINE_NO_LOCAL_CLOCKS = Rule(
    name="tests-define-no-local-clocks",
    find=lambda tree, path: (
        list(_local_clock_offenders(tree, path)) + list(_bound_clock_offenders(tree, path))
    ),
    scope=lambda *, exclude: suite_trees(),
    summary=(
        "every clock-like symbol in tests/** is defined in or re-exported from"
        " tests/_clocks.py -- a second clock home is a second timeline the"
        " arrangement cannot be audited through (#843)"
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic_clock.py",
    bite=lambda: (
        Bites(
            "import time\n\n_FROZEN = time.time\n",
            "binds a clock read",
        ),
        Bites(
            "import time\n\ndef fake_clocked_sleep(delay):\n    return time.time()\n",
            "defines its own clock",
        ),
        Bites(
            "class _Clock:\n    def __call__(self):\n        return 0.0\n",
            "defines its own clock",
        ),
        Bites(
            "import time\n\n_FAKE = time.time\n",
            "binds a clock read",
        ),
    ),
    clean=lambda: (
        # A method is not a module-level def: the level walk spares the class body.
        Silent("class Cache:\n    def clock(self):\n        return 0.0\n"),
        # The invariant suite and the clock home are the law, not the governed.
        Silent("def arrange_clock():\n    return None\n", at=CLOCK_HOME),
        Silent(
            "def clocked_fixture():\n    return None\n",
            at=REPO_ROOT / "tests" / "invariants" / "_synthetic.py",
        ),
        # Path-keyed: the same clock def OUTSIDE tests/** stays quiet -- the
        # law governs the test suite's timelines, not production's.
        Silent("def clocked_fixture():\n    return None\n", at=APP_ROOT / "_synthetic.py"),
        # The elsewhere half (a path no bite claims): a module the sweep owns
        # whose names simply are not clock-like stays quiet.
        Silent("def helper():\n    return 1\n", at=REPO_ROOT / "tests" / "_synthetic_elsewhere.py"),
        # A non-clock binding is not a local clock (the _FAKE shape's clean half).
        Silent("_LIMIT = 10\n"),
    ),
)

RULES = (TESTS_DEFINE_NO_LOCAL_CLOCKS,)


def test_tests_define_no_local_clocks():
    """The suite's one clock home: no clock-like def outside ``_clocks.py``."""
    TESTS_DEFINE_NO_LOCAL_CLOCKS.scan()
