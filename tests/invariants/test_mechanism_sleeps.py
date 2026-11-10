"""Mechanism sleeps: fast_sleeps neuters ``time.sleep`` in spawned threads and
handlers, so a sleep that IS the hold-open mechanism must carry the marker."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import TESTS_DIR, attribute_call_on, offender_at, trees_under
from tests.rules import Bites, Rule

_TIME_LIMIT_NOTE = (
    "time.sleep inside a handler/thread the test spawns IS the hold-open"
    " mechanism -- mark @pytest.mark.real_sleeps or hold with an Event instead"
    " (fast_sleeps neuters it otherwise)"
)


def _marked_real_sleeps(node, parent_class=None) -> bool:
    """real_sleeps via get_closest_marker semantics: the test or its class."""
    scopes = (node,) if parent_class is None else (node, parent_class)
    return any(getattr(d, "attr", None) == "real_sleeps" for scope in scopes for d in scope.decorator_list)


def _nested_defs(fn) -> tuple[ast.AST, ...]:
    """The def/class bodies nested under ``fn`` (the handler/thread shapes)."""
    return tuple(
        node
        for node in ast.walk(fn)
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def _mechanism_sleep_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """``time.sleep`` inside a nested def/class of an unmarked test: the sleep is
    the mechanism (a handler or thread body holding a request open), and
    fast_sleeps' no-op patch silently deletes it -- a bound observed against no
    hold can green with the bound broken."""
    offenders: list[str] = []
    for scope in tree.body:
        if not isinstance(scope, ast.ClassDef):
            continue
        for node in scope.body:
            if not _is_test_def(node):
                continue
            offenders.extend(_test_sleep_offenders(node, path, parent_class=scope))
    for node in tree.body:
        if _is_test_def(node):
            offenders.extend(_test_sleep_offenders(node, path))
    return offenders


def _is_test_def(node) -> bool:
    """A collected test function, module-level or a Test* class method."""
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _test_sleep_offenders(fn, path: pathlib.Path, *, parent_class=None) -> list[str]:
    """Offenders for one test: unmarked time.sleep inside its nested defs."""
    if _marked_real_sleeps(fn, parent_class):
        return []
    nested = {id(n) for n in _nested_defs(fn)}
    return [
        offender_at(path, call, _TIME_LIMIT_NOTE)
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and attribute_call_on(call, "time", "sleep")
        and any(id(d) in nested and call in ast.walk(d) for d in _nested_defs(fn))
    ]


MECHANISM_SLEEPS_CARRY_THE_MARKER = Rule(
    name="mechanism-sleeps-carry-the-marker",
    find=_mechanism_sleep_offenders,
    summary=(
        "a time.sleep in a thread/handler a test spawns is neutered by the"
        " autouse fast_sleeps fixture; a sleep that IS the mechanism needs"
        " @pytest.mark.real_sleeps or an Event-based hold"
    ),
    scope=lambda *, exclude: trees_under(TESTS_DIR),
    proof_path=TESTS_DIR / "_synthetic.py",
    bite=lambda: (
        # The handler-body shape from the #825 finding: sleep inside a nested
        # class of an unmarked test.
        Bites(
            "import time\n"
            "class TestT:\n"
            "    def test_bound(self):\n"
            "        class _Handler:\n"
            "            def do_GET(self):\n"
            "                time.sleep(0.05)\n",
            "IS the hold-open mechanism",
        ),
        # Thread-body spelling: a nested function, not a class.
        Bites(
            "import time\ndef test_overlap(self):\n    def _worker():\n        time.sleep(0.1)\n",
            "IS the hold-open mechanism",
        ),
    ),
    clean=lambda: (
        # Poll-with-deadline directly in the test body: the sleep is a poll, not
        # the mechanism, and the fast patch keeping the loop alive is fine.
        "import time\n"
        "def test_polls(self):\n"
        "    while not done() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n",
        # The marker opts the whole test out of fast_sleeps (class spelling).
        "import time\n"
        "import pytest\n"
        "@pytest.mark.real_sleeps\n"
        "class TestT:\n"
        "    def test_bound(self):\n"
        "        class _Handler:\n"
        "            def do_GET(self):\n"
        "                time.sleep(0.05)\n",
        # asyncio.sleep is out of scope: the fast patch still yields.
        "import asyncio\ndef test_polls(self):\n    async def _w():\n        await asyncio.sleep(0)\n",
        # Module-level helpers with no test ancestor are the doubles' business.
        "import time\ndef helper():\n    time.sleep(0.01)\n",
    ),
)


def test_mechanism_sleeps_carry_the_marker():
    MECHANISM_SLEEPS_CARRY_THE_MARKER.scan()
