"""Module-level mutable state has a reset door: a container bound at module level and mutated by the module's
own functions is process state, and when nothing in the module empties it, a test that leaks state into it
has no public way to clean up.
"""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import offender
from tests.chokepoint_walks import nodes_of
from tests.invariants._shared import function_defs, module_level_bindings
from tests.rules import Bites, Rule

_EMPTIED_BY = frozenset({"clear"})

#: Calls that change the container's contents; every other method on the name
#: (``logger.warning``, ``_PATTERN.match``) is a use, not a mutation.
_MUTATORS = frozenset(
    {
        "add",
        "clear",
        "update",
        "discard",
        "pop",
        "append",
        "remove",
        "setdefault",
        "popitem",
        "insert",
        "extend",
    }
)


def _module_level_containers(tree: ast.Module) -> set[str]:
    """Names bound once at module level (dunder names are machinery, not state)."""
    return {
        name for name in module_level_bindings(tree) if not (name.startswith("__") and name.endswith("__"))
    }


def _receives_mutation(node: ast.AST, containers: set[str]) -> str | None:
    """The container name when ``node`` mutates one, or None."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                if target.value.id in containers:
                    return target.value.id
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in containers and node.func.attr in _MUTATORS:
            return receiver.id
    return None


def _mutations_by_line(tree: ast.Module) -> dict[str, int]:
    """Mutated module-level container -> first line mutating it, from the module's
    own functions. Shared by both detectors: the mutation sweep is one walk."""
    containers = _module_level_containers(tree)
    if not containers:
        return {}
    mutated: dict[str, int] = {}
    for fn in function_defs(tree):
        for node in nodes_of(fn):
            if not isinstance(node, (ast.stmt, ast.expr)):
                continue
            hit = _receives_mutation(node, containers)
            if hit is not None and node.lineno != fn.lineno:
                mutated.setdefault(hit, node.lineno)
    return mutated


def _mutated_without_reset_door_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every mutated module-level container no module-level function ever empties. Only a ``.clear()`` call
    counts -- ``pop``/``discard`` remove one element and a rebind is invisible to holders of the old
    object; the registry module itself is the recursion base case.
    """
    if path.name == "process_state.py":
        return []
    mutated = _mutations_by_line(tree)
    if not mutated:
        return []
    emptied: set[str] = set()
    for fn in function_defs(tree):
        for node in nodes_of(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _EMPTIED_BY
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in mutated
            ):
                emptied.add(node.func.value.id)
    return [
        offender(
            path,
            mutated[name],
            f"{name} is mutated by this module but no function empties it -- add a "
            f"reset door (a function calling {name}.clear()) so the reset family can isolate tests",
        )
        for name in sorted(mutated.keys() - emptied)
    ]


def _hand_copied_pair_offenders() -> list[str]:
    """App modules defining reset_process_state/has_process_state at module level.

    The pair lives once, in ``crawlutron.runtime.process_state``; every owner
    registers private closures with the registry instead of exporting a copy.
    """
    from tests.chokepoint_walks import REPO_ROOT

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "crawlutron").rglob("*.py")):
        if path.name == "process_state.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        hit = names & {"reset_process_state", "has_process_state"}
        if hit:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {sorted(hit)}")
    return offenders


MUTATED_STATE_HAS_A_RESET_DOOR = Rule(
    name="mutated-module-state-has-a-reset-door",
    find=_mutated_without_reset_door_offenders,
    summary=(
        "a module-level container the module mutates needs a reset door -- a function "
        "that empties it -- so tests can isolate and the reset family can own it"
    ),
    bite=lambda: (
        Bites(
            "STATE = {}\n\n\ndef fill():\n    STATE['k'] = 1\n",
            "no function empties it",
        ),
        Bites(
            "SEEN = set()\n\n\ndef mark(x):\n    SEEN.add(x)\n",
            "no function empties it",
        ),
    ),
    clean=lambda: (
        "STATE = {}\n\n\ndef fill():\n    STATE['k'] = 1\n\n\ndef _reset():\n    STATE.clear()\n",
        # Read-only containers are not state: nothing mutates them.
        "TABLE = {}\n\n\ndef lookup(k):\n    return TABLE.get(k)\n",
        # A global rebinding is not container mutation: the name is rebound, not mutated.
        "COUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT = COUNT + 1\n",
    ),
)


def test_mutated_module_state_has_a_reset_door():
    MUTATED_STATE_HAS_A_RESET_DOOR.scan()


def test_reset_door_detector_bites():
    MUTATED_STATE_HAS_A_RESET_DOOR.check_bites()


def test_no_module_hand_copies_the_reset_pair():
    offenders = _hand_copied_pair_offenders()
    assert not offenders, (
        f"the reset family is registry-owned (#621); hand-copied pairs: {offenders} -- "
        f"register private closures with crawlutron.runtime.process_state instead"
    )


def _reset_shaped_but_unregistered_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Modules with a reset-shaped door -- a function emptying a mutated module-level container -- that never
    call ``process_state.register(...)`` in their own body: the call IS the registration, cross-checked
    over the live registry.
    """
    if path.name == "process_state.py":
        return []
    if _mutated_without_reset_door_offenders(tree, path):
        return []  # the door rule already owns this file
    mutated = _mutations_by_line(tree)
    if not mutated:
        return []
    for node in nodes_of(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "register" and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "process_state":
                    return []
    assert mutated
    return [
        offender(
            path,
            min(mutated.values()),
            f"this module holds reset-shaped module state ({', '.join(sorted(mutated))}) but never "
            f"calls process_state.register -- register a reset/probe pair so the reset family owns it",
        )
    ]


RESET_SHAPED_STATE_IS_REGISTERED = Rule(
    name="reset-shaped-state-is-registered",
    find=_reset_shaped_but_unregistered_offenders,
    summary=(
        "a module with a reset door for module-level state must register that state with "
        "crawlutron.runtime.process_state -- an unregistered reset is invisible to the seam"
    ),
    bite=lambda: (
        Bites(
            "STATE = {}\n\n\ndef fill():\n    STATE['k'] = 1\n\n\ndef _reset():\n    STATE.clear()\n",
            "never calls process_state.register",
        ),
    ),
    clean=lambda: (
        # No mutated state, no registration duty.
        "TABLE = {}\n\n\ndef lookup(k):\n    return TABLE.get(k)\n",
    ),
)


def test_reset_shaped_state_is_registered():
    RESET_SHAPED_STATE_IS_REGISTERED.scan()


def test_reset_shaped_registration_bites():
    RESET_SHAPED_STATE_IS_REGISTERED.check_bites()
