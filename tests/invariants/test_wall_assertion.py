"""Wall assertions carry the ``wall_assertion`` marker: a test that reads a wall clock and then asserts on
the elapsed delta is the flake-seed class.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import REPO_ROOT, offender_at
from tests.invariants._clock_family import suite_trees, wall_names, wall_read
from tests.rules import Bites, Rule, Silent

# Wall reads here are arrangement (state construction, never asserted on) or
# sanctioned bounds. The table only ever shrinks: a row deleted here is the ratchet.
_EXEMPT = frozenset(
    {
        "tests/test_safe_regex.py",  # native C-loop timeout bound, marked
        "tests/extraction/test_llm_format.py",  # linear-scan budgets, marked
    }
)


def _marked(node, parent_class=None) -> bool:
    """``wall_assertion`` via get_closest_marker semantics: test or its class."""
    return _marker_spelled(node, "wall_assertion", parent_class)


def _marker_spelled(node, marker: str, parent_class=None) -> bool:
    """The shared marker-scope walk: the test or its class carries ``marker``."""
    scopes = (node,) if parent_class is None else (node, parent_class)
    return any(getattr(d, "attr", None) == marker for scope in scopes for d in scope.decorator_list)


def _test_defs(
    tree: ast.Module,
) -> Iterator[tuple[ast.ClassDef | None, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every collected test def with its class scope, module-level or method."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith(
                    "test"
                ):
                    yield node, child
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield None, node


def _wall_assertion_offenders(tree: ast.Module, path: pathlib.Path) -> Iterator[str]:
    """An unmarked test that reads a wall clock INSIDE an assert/compare: the
    flake-seed class. A read assigned first and asserted later is matched by
    following the read's name to its uses in asserts and comparisons."""
    relative = path.relative_to(REPO_ROOT).as_posix()
    if relative in _EXEMPT or not relative.startswith("tests/"):
        return
    # The law governs the test suite's timelines, not production's; the two
    # marked files carry sanctioned native bounds.
    wall = wall_names(tree)
    for scope, fn in _test_defs(tree):
        if _marked(fn, scope):
            continue
        reads: dict[str, ast.AST] = {}
        reported: set[int] = set()
        arranged = False
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and (wall_read(node.value, wall) or _mentions(node.value, reads, wall))
            ):
                # A direct read or an elapsed-delta computation both seed the
                # name: the later assert reads the wall clock through it.
                reads[node.targets[0].id] = node.targets[0]
                arranged = True
            if (
                isinstance(node, (ast.Assert, ast.Compare))
                and getattr(node, "lineno", None) not in reported
                and _mentions(node, reads, wall)
            ):
                # An Assert and its nested Compare share a line: one finding
                # per line, the duplicated shape is the same finding.
                reported.add(node.lineno)
                yield offender_at(path, node, _DETAIL)
        if reported:
            continue
        if arranged:
            # The read was assigned for arrangement (never asserted on): a
            # wall read the test consumes, not one it bets a claim on.
            continue
        # A test that reads a wall clock AT ALL without the marker is still
        # governed: the read is the seed, the assert is only its flower.
        for node in ast.walk(fn):
            if isinstance(node, ast.expr) and wall_read(node, wall) and not isinstance(node, ast.Call):
                yield offender_at(path, node, _DETAIL)
                return


_DETAIL = (
    "asserts on elapsed wall time: mark @pytest.mark.wall_assertion or assert "
    "the requested wait through an instrument (tests/_clocks.py) (#843)"
)


def _mentions(node: ast.expr | ast.Assert, reads: dict[str, ast.AST], wall: set[str]) -> bool:
    """Whether the expression compares an elapsed delta: a recorded wall read's
    name, or a subtraction of two reads, appears inside it."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub):
            if wall_read(sub.left, wall) or wall_read(sub.right, wall):
                return True
        if isinstance(sub, ast.Name) and sub.id in reads:
            return True
    return False


WALL_ASSERTIONS_CARRY_THE_MARKER = Rule(
    name="wall-assertions-carry-the-marker",
    find=_wall_assertion_offenders,
    scope=lambda *, exclude: suite_trees(),
    summary=(
        "a test asserting on elapsed wall time carries @pytest.mark.wall_assertion"
        " -- an unmarked wall-elapsed assert is the flake-seed class (#843)"
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic_wall.py",
    bite=lambda: (
        Bites(
            "import time\n\ndef test_bound():\n    start = time.perf_counter()\n"
            "    run()\n    assert time.perf_counter() - start < 1.0\n",
            "elapsed wall time",
        ),
        Bites(
            "from time import perf_counter\n\ndef test_bound():\n    started = perf_counter()\n"
            "    run()\n    elapsed = perf_counter() - started\n    assert elapsed < 1.0\n",
            "elapsed wall time",
        ),
    ),
    clean=lambda: (
        # A wall read that is never asserted on is arrangement, not an assertion.
        Silent("import time\n\ndef test_arranges():\n    now = time.time()\n    use(now)\n"),
        # The marker spares the test.
        Silent(
            "import time\nimport pytest\n\n@pytest.mark.wall_assertion\n"
            "def test_bound():\n    start = time.perf_counter()\n"
            "    run()\n    assert time.perf_counter() - start < 1.0\n",
        ),
        # Monotonic is not the governed family here.
        Silent("import time\n\ndef test_polls():\n    t = time.monotonic()\n    assert t > 0\n"),
        # Helper code with no test ancestor is not a test assertion.
        Silent("import time\n\ndef helper():\n    return time.time()\n"),
        # Path-keyed: the same assert outside tests/** stays quiet.
        Silent(
            "import time\n\ndef test_bound():\n    start = time.perf_counter()\n"
            "    assert time.perf_counter() - start < 1.0\n",
            at=REPO_ROOT / "crawlutron" / "_synthetic.py",
        ),
        # The elsewhere half (a path no bite claims): an unmarked test without
        # a wall assert stays quiet.
        Silent("def test_other():\n    assert 1 + 1 == 2\n", at=REPO_ROOT / "tests" / "_synthetic_wall2.py"),
    ),
)


RULES = (WALL_ASSERTIONS_CARRY_THE_MARKER,)


def test_wall_assertions_carry_the_marker():
    """Every wall-elapsed assertion is marked (#843)."""
    WALL_ASSERTIONS_CARRY_THE_MARKER.scan()
