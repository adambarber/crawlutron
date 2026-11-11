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
    from tests.invariants.test_wall_assertion import _marker_spelled

    return _marker_spelled(node, "real_sleeps", parent_class)


def _nested_defs(fn) -> tuple[ast.AST, ...]:
    """The def/class bodies nested under ``fn`` (the handler/thread shapes)."""
    return tuple(
        node
        for node in ast.walk(fn)
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def _time_sleep_attr(node) -> bool:
    """A bare ``time.sleep`` attribute reference."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "sleep"
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
    )


def _sleep_aliases(tree: ast.Module) -> set[str]:
    """Bare names bound to ``time.sleep`` by ``from time import sleep [as x]``."""
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "time"
        for alias in node.names
        if alias.name == "sleep"
    }


def _is_sleep_call(call: ast.Call, aliases: set[str]) -> bool:
    """A call that invokes time.sleep, however spelled."""
    if attribute_call_on(call, "time", "sleep"):
        return True
    if isinstance(call.func, ast.Name) and call.func.id in aliases:
        return True
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "partial"
        and bool(call.args)
        and _sleep_refs(call.args[0], aliases)
    )


def _sleep_refs(node, aliases: set[str]) -> bool:
    """True when ``node`` is or wraps a time.sleep reference: the attribute, an
    aliased bare name, a lambda wrapping a sleep call, or a partial over one."""
    if isinstance(node, ast.Lambda):
        return any(_is_sleep_call(c, aliases) for c in ast.walk(node) if isinstance(c, ast.Call))
    if isinstance(node, ast.Call):
        return _is_sleep_call(node, aliases)
    if isinstance(node, ast.Attribute):
        return _time_sleep_attr(node)
    return isinstance(node, ast.Name) and node.id in aliases


def _spawns_sleep(call: ast.Call, aliases: set[str]) -> bool:
    """A Thread/executor call handing a sleep reference to a spawned body
    (``target=`` keyword, or ``submit(sleep, ...)`` as the first argument).
    The sleep runs in the spawned thread even when the spawn sits in the test
    body, so this shape does not require a nested def. Reported on the spawn
    call itself, so a lambda-wrapped sleep is one offender, not two."""
    if any(kw.arg == "target" and _sleep_refs(kw.value, aliases) for kw in call.keywords):
        return True
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "submit"
        and bool(call.args)
        and _sleep_refs(call.args[0], aliases)
    )


def _test_sleep_aliases(fn, aliases: set[str]) -> set[str]:
    """``aliases`` plus names the test binds to a sleep reference (a named
    lambda, a partial) -- a rename-shaped escape one assignment away."""
    bound = set(aliases)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not _sleep_refs(node.value, bound):
            continue
        bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return bound


def _mechanism_sleep_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """``time.sleep`` inside a nested def/class of an unmarked test: the sleep is
    the mechanism (a handler or thread body holding a request open), and
    fast_sleeps' no-op patch silently deletes it -- a bound observed against no
    hold can green with the bound broken."""
    offenders: list[str] = []
    aliases = _sleep_aliases(tree)
    for scope in tree.body:
        if not isinstance(scope, ast.ClassDef):
            continue
        for node in scope.body:
            if not _is_test_def(node):
                continue
            offenders.extend(_test_sleep_offenders(node, path, parent_class=scope, aliases=aliases))
    for node in tree.body:
        if _is_test_def(node):
            offenders.extend(_test_sleep_offenders(node, path, aliases=aliases))
    return offenders


def _is_test_def(node) -> bool:
    """A collected test function, module-level or a Test* class method."""
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _test_sleep_offenders(fn, path: pathlib.Path, *, aliases: set[str], parent_class=None) -> list[str]:
    """Offenders for one test: unmarked time.sleep inside its nested defs."""
    if _marked_real_sleeps(fn, parent_class):
        return []
    nested = {id(n) for n in _nested_defs(fn)}
    aliases = _test_sleep_aliases(fn, aliases)
    spawns = [call for call in ast.walk(fn) if isinstance(call, ast.Call) and _spawns_sleep(call, aliases)]
    offenders = [offender_at(path, spawn, _TIME_LIMIT_NOTE) for spawn in spawns]
    offenders.extend(
        offender_at(path, call, _TIME_LIMIT_NOTE)
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and call not in spawns
        and not any(spawn is not call and call in ast.walk(spawn) for spawn in spawns)
        and _is_sleep_call(call, aliases)
        and any(id(d) in nested and call in ast.walk(d) for d in _nested_defs(fn))
    )
    return offenders


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
        # Thread(target=time.sleep): a bare reference, no Call node to match.
        Bites(
            "import threading\nimport time\n"
            "def test_overlap(self):\n"
            "    def _spawn():\n"
            "        threading.Thread(target=time.sleep).start()\n",
            "IS the hold-open mechanism",
        ),
        # Lambda-wrapped sleep: the Call hides inside the lambda body.
        Bites(
            "import threading\nimport time\n"
            "def test_overlap(self):\n"
            "    def _spawn():\n"
            "        threading.Thread(target=lambda: time.sleep(0.1)).start()\n",
            "IS the hold-open mechanism",
        ),
        # from time import sleep: the call site is a bare Name, not an attribute.
        Bites(
            "from time import sleep\ndef test_overlap(self):\n    def _worker():\n        sleep(0.1)\n",
            "IS the hold-open mechanism",
        ),
        # functools.partial over time.sleep handed to a thread.
        Bites(
            "import functools\nimport threading\nimport time\n"
            "def test_overlap(self):\n"
            "    threading.Thread(target=functools.partial(time.sleep, 5))\n",
            "IS the hold-open mechanism",
        ),
        # A named alias to the sleep lambda -- the rename one binding away.
        Bites(
            "import threading\nimport time\n"
            "def test_overlap(self):\n"
            "    body = lambda: time.sleep(0.1)\n"
            "    threading.Thread(target=body)\n",
            "IS the hold-open mechanism",
        ),
        # Executor submit with sleep as the callable.
        Bites(
            "from concurrent.futures import ThreadPoolExecutor\nimport time\n"
            "def test_overlap(self):\n"
            "    ThreadPoolExecutor().submit(time.sleep, 5)\n",
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
        # partial/aliased sleeps at MODULE scope (no test ancestor) stay exempt
        # like any module-level helper -- the exemption predates the widening.
        "import time\nimport functools\npartial_sleep = functools.partial(time.sleep, 5)\n",
        "from time import sleep as rest\ndef helper():\n    rest(0.01)\n",
        # time.monotonic and asyncio.sleep stay out of scope.
        "import time\ndef test_polls(self):\n    t = time.monotonic()\n    assert t > 0\n",
    ),
)


def test_mechanism_sleeps_carry_the_marker():
    MECHANISM_SLEEPS_CARRY_THE_MARKER.scan()
