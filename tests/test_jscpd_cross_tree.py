"""Clones spanning trees fail; none are carried by content or guard."""

from __future__ import annotations

import pytest

from scripts import jscpd_gate as gate
from scripts._jscpd_report_types import JscpdDuplicate
from scripts.executables import require_repo_tool
from tests._jscpd_gate_helpers import (
    _args,
    _duplicate,
    _run_against_empty_tree,
    _stub_report,
)


def test_uniform_profile_shape() -> None:
    assert set(gate.PROFILE_PATHS) == {"all"}
    assert gate.MIN_SOURCES == {"all": 200}


def _cross_tree_duplicate() -> JscpdDuplicate:
    """A fragment shared between production and a test, at the same floor.

    A harness re-spelling production's truth looks like one legitimate copy on
    each side, so a per-tree gate has nothing to object to even in principle."""
    return _duplicate(
        "packages/core/src/crawlutron/orchestration/recursive.py", "tests/_recursive_harnesses.py"
    )


def test_a_clone_spanning_two_trees_fails(monkeypatch, capsys) -> None:
    _stub_report(monkeypatch, [_cross_tree_duplicate()])

    assert gate._run(_args()) == 1
    error = capsys.readouterr().err
    assert "[cross]" in error
    assert "packages/core/src/crawlutron/orchestration/recursive.py" in error
    assert "tests/_recursive_harnesses.py" in error


def test_a_pair_inside_one_tree_is_not_also_reported_as_cross_tree(monkeypatch, capsys) -> None:
    """A within-tree pair is scored once and stays in its own tree's column.

    In the single whole-repo report every pair is reported once; a within-tree
    pair is attributed to its tree, never also to the cross column."""
    _stub_report(
        monkeypatch,
        [
            {
                "firstFile": {"name": "packages/core/src/crawlutron/storage/ids.py", "start": 10, "end": 20},
                "secondFile": {"name": "packages/core/src/crawlutron/storage/ids.py", "start": 30, "end": 40},
                "fragment": "within-app-only",
            }
        ],
    )

    assert gate._run(_args()) == 1
    error = capsys.readouterr().err
    assert "[production]" in error
    assert "[cross]" not in error


def test_jscpd_command_resolves_the_service_copy(monkeypatch) -> None:
    """The command names the repository's own jscpd, whichever install it came from.

    The install is two-shaped (.venv locally, console script in CI); spelling
    the venv path made the assertion false in CI for a correct command."""
    commands = _run_against_empty_tree(monkeypatch)
    assert commands
    for command in commands:
        assert command[0] == require_repo_tool(gate.SERVICE_ROOT, "jscpd")
        assert "--fail-on-empty" in command
        assert command[command.index("--threshold") + 1] == "0"


@pytest.mark.parametrize("directory", ("packages/core", "tests", "scripts"))
def test_duplicate_in_each_service_tree_fails(monkeypatch, directory):
    _stub_report(
        monkeypatch,
        [_duplicate(f"{directory}/one.py", f"{directory}/two.py")],
    )

    assert gate._run(_args()) == 1
