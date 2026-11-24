"""Unmediated task/connection lifecycle operations are a banned alternate surface.

Only the TaskScope door admits spawn/stop; alternates are ratcheted, narrowings named here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._walk_primitives import SCAN_ROOTS
from tests.chokepoint_paths import REPO_ROOT, relative_posix
from tests.chokepoint_walks import (
    canonical_tree,
    nodes_of,
    offender_at,
    parent_map,
    parsed_source,
    resolved_call_name,
    resolved_expr_name,
    single_target_name_assignments,
)

_SPAWN_LEAVES = frozenset({"create_task", "ensure_future"})
_DOOR_PATHS = frozenset(
    {
        "packages/crawlutron-admit/src/crawlutron_admit/task_scope.py",
        "packages/core/src/crawlutron/service/job_lifecycle.py",
    }
)
# The door's conformance suite constructs raw tasks to prove stop cancels them.
_DOOR_TEST_FILES = frozenset({"tests/admission/test_task_scope.py"})
_BAN_RECEIVERS = ("task", "runner", "handler", "probe", "fetch", "forward", "closing_task")
_NARROW_RECEIVERS = ("timer", "future", "concurrent_future")
_MONKEY_LEAVES = frozenset({"setattr", "setitem"})
_SEAM_TOKENS = (
    "shutdown",
    "close",
    "aclose",
    "cancel",
    "stop",
    "drain",
    "create_task",
    "ensure_future",
    "retire",
)
_NOOP_TOKENS = ("noop", "no_op", "null", "stub")
_MOCK_LEAVES = frozenset({"Mock", "MagicMock", "AsyncMock", "coroutine"})

_PROBE_PATH = REPO_ROOT / "tests" / "_lifecycle_probe.py"


def _leaf(call: ast.Call) -> str:
    func = call.func
    return getattr(func, "id", None) or getattr(func, "attr", None) or ""


def _call_name(tree: ast.Module, call: ast.Call) -> str:
    return resolved_call_name(tree, call)


def _resolved_leaf(tree: ast.Module, call: ast.Call) -> str:
    return _call_name(tree, call).rsplit(".", 1)[-1]


def _alias_names(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node, name in single_target_name_assignments(nodes_of(tree), lambda _: True):
        resolved = resolved_expr_name(tree, node.value).lower()
        if resolved:
            aliases[name] = resolved
    return aliases


def _base_expr(expr: ast.expr) -> ast.expr:
    if isinstance(expr, ast.Subscript):
        return _base_expr(expr.value)
    return expr


def _banned_expr(tree: ast.Module, expr: ast.expr, aliases: dict[str, str]) -> bool:
    base = _base_expr(expr)
    if isinstance(base, ast.Call):
        return _banned_receiver(_call_name(tree, base).rsplit(".", 1)[-1].lower())
    if _banned_receiver(resolved_expr_name(tree, base).lower()):
        return True
    alias = aliases.get(base.id) if isinstance(base, ast.Name) else None
    return bool(alias and _banned_receiver(alias))


def _is_door_file(relative: str) -> bool:
    return relative in _DOOR_PATHS


def _banned_receiver(name: str) -> bool:
    return any(token in name for token in _BAN_RECEIVERS) and not any(
        token in name for token in _NARROW_RECEIVERS
    )


def _in_admitted(tree: ast.Module, node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.ClassDef) and "taskscope" in current.name.lower():
            return True
        if isinstance(current, (ast.With, ast.AsyncWith)):
            for item in current.items:
                context = item.context_expr
                name = (
                    _call_name(tree, context)
                    if isinstance(context, ast.Call)
                    else resolved_expr_name(tree, context)
                )
                if "taskscope" in name.lower().replace("_", ""):
                    return True
        current = parents.get(id(current))
    return False


def _spawn_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    if _is_door_file(relative) or relative in _DOOR_TEST_FILES:
        return []
    parents = parent_map(tree)
    return [
        offender_at(path, node, "unmediated task spawn")
        for node in nodes_of(tree)
        if isinstance(node, ast.Call)
        and _resolved_leaf(tree, node) in _SPAWN_LEAVES
        and not _in_admitted(tree, node, parents)
    ]


def _status_bypass(node: ast.Call) -> bool:
    if _leaf(node) != "set_status":
        return False
    values = [*node.args, *(keyword.value for keyword in node.keywords)]
    return any("CANCELLED" in ast.unparse(value).upper() for value in values)


def _wait_for_retirement(tree: ast.Module, node: ast.Call, aliases: dict[str, str]) -> bool:
    if _leaf(node) != "wait_for" or not node.args:
        return False
    return _banned_expr(tree, node.args[0], aliases)


def _retire_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    if _is_door_file(relative) or relative in _DOOR_TEST_FILES:
        return []
    aliases = _alias_names(tree)
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "cancel"
            and _banned_expr(tree, func.value, aliases)
        ):
            offenders.append(offender_at(path, node, "bare lifecycle cancel"))
        elif "job_store" not in relative and _status_bypass(node):
            offenders.append(offender_at(path, node, "status-plane bypass"))
        elif _wait_for_retirement(tree, node, aliases):
            offenders.append(offender_at(path, node, "wait_for as retirement"))
    return offenders


def _literal(expr: ast.AST) -> str:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return ""


def _is_noop(expr: ast.AST) -> bool:
    if isinstance(expr, ast.Name):
        return any(token in expr.id.lower() for token in _NOOP_TOKENS) or expr.id == "None"
    if isinstance(expr, ast.Constant):
        return expr.value is None or isinstance(expr.value, type(Ellipsis))
    if isinstance(expr, ast.Lambda):
        body = expr.body
        if isinstance(body, ast.Constant):
            return body.value is None or isinstance(body.value, type(Ellipsis))
        return isinstance(body, ast.Name) and body.id == "None"
    if isinstance(expr, ast.Call):
        return _leaf(expr) in _MOCK_LEAVES
    return False


def _is_bare_spawn_replacement(tree: ast.Module, expr: ast.AST) -> bool:
    if not isinstance(expr, ast.Name):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == expr.id
        and any(
            isinstance(inner, ast.Call) and _resolved_leaf(tree, inner) in _SPAWN_LEAVES
            for inner in nodes_of(node)
        )
        for node in nodes_of(tree)
    )


def _substitution_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 3:
            continue
        name = _call_name(tree, node)
        if "monkeypatch" not in name.lower() or _leaf(node) not in _MONKEY_LEAVES:
            continue
        target = _literal(node.args[1]).lower()
        if not any(token in target for token in _SEAM_TOKENS):
            continue
        replacement = node.args[2]
        if _is_noop(replacement) or _is_bare_spawn_replacement(tree, replacement):
            offenders.append(offender_at(path, node, "lifecycle seam substituted"))
    return offenders


_DETECTORS = {
    "spawn": _spawn_offenders,
    "retire": _retire_offenders,
    "substitution": _substitution_offenders,
}
_GROUPS = ("load_bearing", "removable")


def _module_paths() -> list[pathlib.Path]:
    return [
        path
        for root in SCAN_ROOTS
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def _group(relative: str) -> str:
    return "load_bearing" if "/src/" in f"/{relative}" else "removable"


def _measured() -> dict[str, int]:
    counts = {f"{kind}.{group}": 0 for kind in _DETECTORS for group in _GROUPS}
    for path in _module_paths():
        tree = canonical_tree(path)
        group = _group(relative_posix(path))
        for kind, detector in _DETECTORS.items():
            counts[f"{kind}.{group}"] += len(detector(tree, path))
    return counts


# Detector-correction re-seed: wider detection (subscript/alias/call receivers,
# import aliases, folded status, path doors); all rows >= prior. Migration moves DOWN.
_RATCHET = {
    "spawn.load_bearing": 10,
    "spawn.removable": 68,
    "retire.load_bearing": 8,
    "retire.removable": 57,
    "substitution.load_bearing": 0,
    # #1404: five blanket closer no-ops became recording, resource-releasing doubles.
    "substitution.removable": 1,
}


def _row_offenders(row: str) -> list[str]:
    kind, group = row.split(".")
    detector = _DETECTORS[kind]
    return [
        offender
        for path in _module_paths()
        if _group(relative_posix(path)) == group
        for offender in detector(canonical_tree(path), path)
    ]


@pytest.mark.parametrize("row", sorted(_RATCHET))
def test_lifecycle_sites_never_exceed_the_ratchet(row: str) -> None:
    found = _measured()[row]
    assert found <= _RATCHET[row], f"{row}: {found} alternate sites exceed {_RATCHET[row]}:\n" + "\n  ".join(
        _row_offenders(row)
    )


@pytest.mark.parametrize("row", sorted(_RATCHET))
def test_the_ratchet_is_tight(row: str) -> None:
    found = _measured()[row]
    assert _RATCHET[row] <= found, f"{row}: ratchet {_RATCHET[row]} is stale; tree has {found}"


def test_the_gate_walks_every_derived_root() -> None:
    assert len(SCAN_ROOTS) >= 4
    assert len(_module_paths()) > 1000
    assert sum(_measured().values()) > 0


def _probe(source: str) -> dict[str, list[str]]:
    tree = parsed_source(source, filename=str(_PROBE_PATH))
    return {kind: detector(tree, _PROBE_PATH) for kind, detector in _DETECTORS.items()}


def test_each_violation_class_bites_by_file_and_line() -> None:
    spawn = _probe("import asyncio\nasync def f():\n    asyncio.create_task(g())\n")
    assert spawn["spawn"] == ["tests/_lifecycle_probe.py:3 unmediated task spawn"]
    cancel = _probe("async def f(handler):\n    handler.cancel()\n")
    assert cancel["retire"] == ["tests/_lifecycle_probe.py:2 bare lifecycle cancel"]
    status = _probe("def f(job_store):\n    job_store.set_status('j', JobStatus.CANCELLED)\n")
    assert status["retire"] == ["tests/_lifecycle_probe.py:2 status-plane bypass"]
    substitute = _probe(
        "def test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'shutdown_job_runner', _noop)\n"
    )
    assert substitute["substitution"] == ["tests/_lifecycle_probe.py:2 lifecycle seam substituted"]


def test_evasion_shapes_bite_by_file_and_line() -> None:
    subscript = _probe("async def f(tasks):\n    tasks[key].cancel()\n")
    assert subscript["retire"] == ["tests/_lifecycle_probe.py:2 bare lifecycle cancel"]
    alias = _probe("async def f():\n    t = get_handler()\n    t.cancel()\n")
    assert alias["retire"] == ["tests/_lifecycle_probe.py:3 bare lifecycle cancel"]
    imported = _probe("from asyncio import create_task as ct\ndef f():\n    ct(g())\n")
    assert imported["spawn"] == ["tests/_lifecycle_probe.py:3 unmediated task spawn"]
    lowercase = _probe("def f(store):\n    store.set_status('j', JobStatus.cancelled)\n")
    assert lowercase["retire"] == ["tests/_lifecycle_probe.py:2 status-plane bypass"]
    wf_sub = _probe("async def f(tasks):\n    await asyncio.wait_for(tasks[key])\n")
    assert wf_sub["retire"] == ["tests/_lifecycle_probe.py:2 wait_for as retirement"]
    wf_call = _probe("async def f():\n    await asyncio.wait_for(get_handler())\n")
    assert wf_call["retire"] == ["tests/_lifecycle_probe.py:2 wait_for as retirement"]


def test_door_exemption_is_by_relative_path_not_basename() -> None:
    source = "async def f():\n    asyncio.create_task(g())\n"
    decoy = REPO_ROOT / "tests" / "decoys" / "task_scope.py"
    assert _spawn_offenders(parsed_source(source, filename=str(decoy)), decoy) == [
        "tests/decoys/task_scope.py:2 unmediated task spawn"
    ]
    door = REPO_ROOT / "packages/crawlutron-admit/src/crawlutron_admit/task_scope.py"
    assert _spawn_offenders(parsed_source(source, filename=str(door)), door) == []


def test_door_test_admission_is_by_relative_path_not_basename() -> None:
    source = "async def f():\n    asyncio.create_task(g())\n"
    decoy = REPO_ROOT / "tests" / "decoys" / "test_task_scope.py"
    assert _spawn_offenders(parsed_source(source, filename=str(decoy)), decoy) == [
        "tests/decoys/test_task_scope.py:2 unmediated task spawn"
    ]
    cancel_source = "async def f(handler):\n    handler.cancel()\n"
    assert _retire_offenders(parsed_source(cancel_source, filename=str(decoy)), decoy) == [
        "tests/decoys/test_task_scope.py:2 bare lifecycle cancel"
    ]
    admitted = REPO_ROOT / "tests" / "admission" / "test_task_scope.py"
    assert _spawn_offenders(parsed_source(source, filename=str(admitted)), admitted) == []
    assert _retire_offenders(parsed_source(cancel_source, filename=str(admitted)), admitted) == []


def test_framework_narrowings_and_the_door_do_not_fire() -> None:
    admitted = _probe(
        "async def f(scope):\n"
        "    async with TaskScope() as scope:\n"
        "        scope.spawn(make)\n"
        "        await scope.stop(handle)\n"
        "        asyncio.create_task(make())\n"
        "    timer.cancel()\n"
        "    future.cancel()\n"
        "    await response.aclose()\n"
    )
    assert not any(admitted.values())
    door = _probe("async def f():\n    asyncio.create_task(g())\n")
    door_tree = parsed_source("async def f():\n    asyncio.create_task(g())\n")
    door_path = REPO_ROOT / "packages/crawlutron-admit/src/crawlutron_admit/task_scope.py"
    assert _spawn_offenders(door_tree, door_path) == []
    assert door["spawn"]
