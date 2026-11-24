"""Bare ``json.loads`` is an unmediated parse edge; all app-tree occurrences route
through :func:`crawlutron.safety.json_safe.parse_json`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._walk_primitives import canonical_tree, parsed_source
from tests._walk_resolution import resolved_call_name
from tests.chokepoint_paths import REPO_ROOT, relative_posix
from tests.chokepoint_walks import app_trees, call_nodes, offender_at

_PROBE_PATH = REPO_ROOT / "tests" / "_json_loads_probe.py"

#: The door module is itself free of bare ``json.loads``; it is exempted by exact
#: relative path, never by basename.
_DOOR_PATHS = frozenset(
    {
        "packages/core/src/crawlutron/safety/json_safe.py",
    }
)


def _json_loads_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    if relative in _DOOR_PATHS:
        return []
    return [
        offender_at(path, node, "bare json.loads outside the parse_json door")
        for node in call_nodes(tree)
        if resolved_call_name(tree, node) == "json.loads"
    ]


_DETECTORS = {"json_loads": _json_loads_offenders}
_GROUPS = ("load_bearing",)


def _module_paths() -> list[pathlib.Path]:
    return [path for path, _ in app_trees()]


def _group(relative: str) -> str:
    return "load_bearing"


def _measured() -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in _DETECTORS:
        for group in _GROUPS:
            counts[f"{kind}.{group}"] = 0
    for path, tree in app_trees():
        for kind, detector in _DETECTORS.items():
            counts[f"{kind}.load_bearing"] += len(detector(tree, path))
    return counts


# Detector-correction re-seed: down-only. Stored-blob narrowings are budgeted,
# not exempted wholesale, so every new bare ``json.loads`` is a red gate.
_RATCHET = {
    "json_loads.load_bearing": 20,
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
def test_json_loads_sites_never_exceed_the_ratchet(row: str) -> None:
    found = _measured()[row]
    assert found <= _RATCHET[row], (
        f"{row}: {found} bare json.loads sites exceed {_RATCHET[row]}:\n" + "\n  ".join(_row_offenders(row))
    )


@pytest.mark.parametrize("row", sorted(_RATCHET))
def test_the_json_loads_ratchet_is_tight(row: str) -> None:
    found = _measured()[row]
    assert _RATCHET[row] <= found, f"{row}: ratchet {_RATCHET[row]} is stale; tree has {found}"


def test_the_gate_walks_every_app_tree() -> None:
    paths = [path for path, _ in app_trees()]
    assert len(paths) > 100
    assert sum(_measured().values()) > 0


def _probe(source: str) -> dict[str, list[str]]:
    tree = parsed_source(source, filename=str(_PROBE_PATH))
    return {kind: detector(tree, _PROBE_PATH) for kind, detector in _DETECTORS.items()}


def test_each_violation_class_bites_by_file_and_line() -> None:
    found = _probe("import json\ndef f():\n    data = json.loads('{}')\n")
    assert found["json_loads"] == ["tests/_json_loads_probe.py:3 bare json.loads outside the parse_json door"]


def test_door_exemption_is_by_relative_path_not_basename() -> None:
    source = "import json\ndef f():\n    data = json.loads('{}')\n"
    decoy = REPO_ROOT / "tests" / "decoys" / "json_safe.py"
    assert _json_loads_offenders(parsed_source(source, filename=str(decoy)), decoy) == [
        "tests/decoys/json_safe.py:3 bare json.loads outside the parse_json door"
    ]
    door = REPO_ROOT / "packages/core/src/crawlutron/safety/json_safe.py"
    assert _json_loads_offenders(parsed_source(source, filename=str(door)), door) == []
