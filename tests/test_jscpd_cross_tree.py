"""Clones spanning trees fail; sanctions pin fragments by hash."""

import pytest

from scripts import jscpd_gate as gate
from scripts.executables import require_repo_tool
from tests.test_jscpd_gate import (
    _args,
    _configure,
    _duplicate,
    _expect_nothing_sanctioned,
    _run_against_empty_tree,
    _valid_pair,
)


def test_uniform_profile_shape() -> None:
    assert set(gate.PROFILE_PATHS) == {"all"}
    assert gate.MIN_SOURCES == {"all": 200}


def _cross_tree_duplicate() -> dict:
    """A fragment shared between production and a test, at the same floor.

    The shape this is about is a test harness re-spelling production's truth: it
    looks like one legitimate copy on each side, so a per-tree gate has nothing to
    object to even in principle.
    """
    return _duplicate("app/orchestration/recursive.py", "tests/_recursive_harnesses.py")


def _sanctioned_cross_tree_pair() -> dict:
    """A declared cross-tree pair, pinned at the locations the duplicate reports.

    The locations are not incidental. ``_expected_tree`` reads the tree label off
    the two declared paths, so a declaration whose sides are in the same tree is
    keyed under that tree and will never be found by the cross-tree pass -- the
    one way to write an expectation here that reads as satisfied and pins
    nothing.
    """
    pair = _valid_pair()
    pair["first"] = {"path": "app/orchestration/recursive.py", "start": 10, "end": 20}
    pair["second"] = {"path": "tests/_recursive_harnesses.py", "start": 30, "end": 40}
    return pair


def _stubbed_scan(duplicates: list[dict], sources: int = 400) -> dict[str, object]:
    """A cross-tree scan result, for the cases that replace the real one."""
    return {"statistics": {"total": {"sources": sources}}, "duplicates": duplicates}


def _without_cross_tree_scan(monkeypatch, duplicates: list[dict], sources: int = 400) -> None:
    monkeypatch.setattr(gate, "_cross_tree_scan", lambda floor, args: _stubbed_scan(duplicates, sources))


def test_a_clone_spanning_two_trees_fails(monkeypatch, tmp_path, capsys) -> None:
    _expect_nothing_sanctioned(monkeypatch, tmp_path, [_cross_tree_duplicate()])

    assert gate._run(_args()) == 1
    error = capsys.readouterr().err
    assert "[cross]" in error
    assert "app/orchestration/recursive.py" in error
    assert "tests/_recursive_harnesses.py" in error


def test_per_root_scans_alone_cannot_see_a_cross_tree_clone(monkeypatch, tmp_path) -> None:
    """The gap this pass exists to close, asserted rather than remembered.

    Runs the same planted cross-tree duplicate with the cross-tree scan removed
    and requires the gate to pass. If the per-root scans ever grow the ability to
    compare across roots -- jscpd adds a flag, the root list is folded into one
    argument -- this fails, and the right response is to drop the spanning
    invocation rather than keep paying for a second full scan of every file on
    every run.
    """
    _expect_nothing_sanctioned(monkeypatch, tmp_path, [_cross_tree_duplicate()])
    _without_cross_tree_scan(monkeypatch, [])

    assert gate._run(_args()) == 0


def test_a_pair_inside_one_tree_is_not_also_reported_as_cross_tree(monkeypatch, tmp_path, capsys) -> None:
    """A within-tree pair is scored once, by the pass that owns its tree.

    The spanning invocation necessarily re-reports every within-tree clone it
    sees -- all three of this service's real pairs are same-file pairs in `app/`.
    Charging those against a second expectation would let one clone satisfy two,
    and would make the cross-tree count a second tally of findings already
    counted rather than a count of the ones nothing else can see.

    The names are real paths because the gate resolves a reported name to a root
    against the filesystem; a synthetic name in a hand-written report is
    unattributable, and unattributable is rejected rather than guessed.
    """
    _expect_nothing_sanctioned(monkeypatch, tmp_path, [])
    _without_cross_tree_scan(
        monkeypatch,
        [
            {
                "firstFile": {"name": "storage/ids.py", "start": 10, "end": 20},
                "secondFile": {"name": "storage/ids.py", "start": 30, "end": 40},
                "fragment": "within-app-only",
            }
        ],
    )

    assert gate._run(_args()) == 0
    assert "[cross] 0 pairs" in capsys.readouterr().out


def test_a_cross_tree_clone_can_be_sanctioned_with_a_guard(monkeypatch, tmp_path) -> None:
    """A cross-tree clone is sanctionable on the same terms as any other.

    Not a concession: a guard is the only thing that ever justified a sanctioned
    pair, and refusing cross-tree pairs specifically would make a justified one
    unsanctionable -- so the fix would be to delete the duplication, which is the
    outcome the gate wants anyway, by making it impossible rather than by
    disallowing the admission.
    """
    _configure(monkeypatch, tmp_path, _sanctioned_cross_tree_pair(), [_cross_tree_duplicate()])

    assert gate._run(_args()) == 0


def test_a_cross_tree_pair_whose_fragment_moved_is_drift(monkeypatch, tmp_path, capsys) -> None:
    """Attribution follows the content across both trees, as for a within-tree pair.

    A sanctioned cross-tree pair identified by path and line would read as a
    disappearance the moment the production side shifted, and the fix-forward
    would be to copy the coordinates out of the error into the expectation -- the
    same failure mode the within-tree content identity exists to prevent.
    """
    moved = _cross_tree_duplicate()
    moved["firstFile"]["start"] = 11
    moved["firstFile"]["end"] = 21
    _configure(monkeypatch, tmp_path, _sanctioned_cross_tree_pair(), [moved])

    assert gate._run(_args()) == 1
    assert "drift" in capsys.readouterr().err


def test_jscpd_command_resolves_the_service_copy(monkeypatch, tmp_path) -> None:
    """The command names the repository's own jscpd, whichever install it came from.

    Asserted against ``require_repo_tool`` rather than against a spelled-out
    path, because the install is genuinely two-shaped: a repository-root
    ``.venv`` locally, the runner's console script in CI. Spelling the venv
    path here made the assertion false in CI for a command that was correct,
    which is the same mistake the gate itself was making.
    """
    commands = _run_against_empty_tree(monkeypatch, tmp_path)
    assert commands
    for command in commands:
        assert command[0] == require_repo_tool(gate.SERVICE_ROOT, "jscpd")
        assert "--fail-on-empty" in command
        assert command[command.index("--threshold") + 1] == "0"
    # The floor each command carries is checked across all roots by test_one_floor_is_applied_to_every_root;
    # per-root floors no longer exist.


@pytest.mark.parametrize("directory", ("app", "tests", "scripts"))
def test_undeclared_clone_in_each_service_tree_fails(monkeypatch, tmp_path, directory):
    _expect_nothing_sanctioned(
        monkeypatch,
        tmp_path,
        [_duplicate(f"{directory}/one.py", f"{directory}/two.py")],
    )

    assert gate._run(_args()) == 1


def test_missing_sanctioned_clone_fails(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, _valid_pair(), [])
    assert gate.main() == 1


# One property: an expectation the gate cannot act on must fail the run, and the only field that varies is the
# one carrying the defect.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", " "),
        ("guard", "tests/invariants/test_taxonomy.py::test_does_not_exist"),
        ("profiles", ["all"]),
    ],
)
def test_expectations_reject_unusable_attestations(monkeypatch, tmp_path, field, value) -> None:
    pair = _valid_pair()
    pair[field] = value
    _configure(monkeypatch, tmp_path, pair, [_duplicate()])
    assert gate.main() == 1


def test_expectation_rejects_changed_fragment(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, _valid_pair(), [_duplicate(fragment="changed")])
    assert gate.main() == 1


def test_valid_sanctioned_pair_passes(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, _valid_pair(), [_duplicate()])
    assert gate.main() == 0
