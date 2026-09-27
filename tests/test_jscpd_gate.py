"""Regression tests for the duplication gate's fail-closed wrapper."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import _jscpd_gate as gate
import pytest


def _args(threshold: float = 0) -> argparse.Namespace:
    return _args_for(threshold=threshold)


_CONFIGURED_MIN_LINES, _CONFIGURED_MIN_TOKENS, _CONFIGURED_THRESHOLD = gate.configured_floor()
"""The floor under test, read from the hook rather than restated here.

This file used to spell 4/39 in ``_args_for`` and again in ``_GATE_ARGV``, and
``.pre-commit-config.yaml`` spelled it a third time in the entry and a fourth in
the hook's name. Four copies of a number, with nothing comparing them, is how the
tests kept passing against a floor the hook no longer ran at -- the suite would
have gone on asserting behaviour at 4/39 while every commit was gated at whatever
the config said. ``gate.configured_floor()`` is the one read, so tightening the
hook tightens the tests in the same edit.
"""


def _args_for(
    threshold: float = _CONFIGURED_THRESHOLD,
    *,
    min_lines: int = _CONFIGURED_MIN_LINES,
    min_tokens: int = _CONFIGURED_MIN_TOKENS,
) -> argparse.Namespace:
    return argparse.Namespace(
        profile="all",
        min_lines=min_lines,
        min_tokens=min_tokens,
        threshold=threshold,
        fail_on_empty=True,
    )


def _write_expectations(path: Path, pairs: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "pairs": pairs}), encoding="utf-8")


def _strip_root(name: str, root: str) -> str:
    """jscpd's name for a file when several roots are scanned in one invocation.

    A single-root invocation reports paths relative to that root. Handed several,
    it strips the root prefix from each and reports `crawl/recursive.py` for
    `app/crawl/recursive.py` and `_recursive_harnesses.py` for
    `tests/_recursive_harnesses.py` -- measured, not assumed: a multi-path scan of
    this service returns exactly those two strings, neither of which names the
    tree it came from. The stub has to reproduce that, because the gate resolves
    those names back to a root against the filesystem and a stub that returned
    honest paths would be modelling a tool that does not exist.
    """
    return name[len(root) + 1 :] if name.startswith(root + "/") else name


def _stripped(location: dict, root: str) -> dict:
    return {**location, "name": _strip_root(str(location["name"]), root)}


def _stub_report(
    monkeypatch,
    duplicates: list[dict],
    percentage: float = 0,
    sources: int | dict[str, int] = 200,
) -> list[list[str]]:
    commands: list[list[str]] = []

    # The gate resolves a reported name back to a root against the filesystem, so
    # the stub has to make its synthetic files exist -- for exactly the roots the
    # duplicates were declared under, so a name in two declared roots is as
    # ambiguous here as it would be on disk. Anything the test did not declare
    # falls through to the real filesystem, which is what keeps an ambiguity like
    # `zz_tree_filler.py` a real one.
    declared = {
        tuple(duplicate[side]["name"].split("/"))
        for duplicate in duplicates
        for side in ("firstFile", "secondFile")
    }

    def _is_file(path: Path) -> bool:
        try:
            relative = tuple(path.relative_to(gate.SERVICE_ROOT).parts)
        except ValueError:
            return False
        return relative in declared or Path.is_file(path)

    monkeypatch.setattr(gate, "_IS_FILE", _is_file)

    def run(command, **_kwargs):
        commands.append(command)
        positionals = command[command.index("--fail-on-empty") + 1 :]
        roots = [argument for argument in positionals if argument in _KNOWN_ROOTS]

        def _inside(name: str) -> bool:
            return any(name == root or name.startswith(root + "/") for root in roots)

        def _straddles(duplicate: dict) -> bool:
            sides = [duplicate[side]["name"] for side in ("firstFile", "secondFile")]
            return len({_root_of(side) for side in sides}) > 1

        def _root_of(name: str) -> str:
            return next(root for root in roots if name == root or name.startswith(root + "/"))

        if len(roots) == 1:
            # One root per invocation: only pairs inside it are reported, named
            # relative to it.
            scoped = [
                {
                    **duplicate,
                    "firstFile": _stripped(duplicate["firstFile"], roots[0]),
                    "secondFile": _stripped(duplicate["secondFile"], roots[0]),
                }
                for duplicate in duplicates
                if _inside(duplicate["firstFile"]["name"])
                and _inside(duplicate["secondFile"]["name"])
            ]
        else:
            # The cross-tree pass: pairs that SPAN roots, named without their root
            # so they are unattributable until the gate resolves them.
            scoped = [
                {
                    **duplicate,
                    "firstFile": _stripped(duplicate["firstFile"], _root_of(duplicate["firstFile"]["name"])),
                    "secondFile": _stripped(duplicate["secondFile"], _root_of(duplicate["secondFile"]["name"])),
                }
                for duplicate in duplicates
                if _straddles(duplicate)
            ]
        if isinstance(sources, dict):
            # One root per invocation is scored at that root's own count, which is
            # what makes "this tree scanned nothing" detectable. The cross-tree
            # invocation reads the same files a second time and its count is
            # deliberately not added to the gate's total, so any positive count
            # satisfies the floor it checks.
            found = sources[roots[0]] if len(roots) == 1 else sources.get(".", max(sources.values()))
        else:
            found = sources
        report = {
            "statistics": {
                "total": {"sources": found, "percentage": percentage, "duplicatedLines": 5 * len(scoped)}
            },
            "duplicates": scoped,
        }
        output = Path(command[command.index("--output") + 1])
        output.mkdir(exist_ok=True)
        (output / "jscpd-report.json").write_text(json.dumps(report), encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gate.subprocess, "run", run)
    return commands


def _command_for(commands: list[list[str]], root: str) -> list[str]:
    return next(
        command
        for command in commands
        if (command[command.index("--fail-on-empty") + 1 :] or [None])[-1] == root
    )


def _run_against_empty_tree(monkeypatch, tmp_path, **kwargs) -> list[list[str]]:
    """Run the gate over a tree with no duplicates; return the commands issued.

    The clean run is the precondition for most of what this file asserts (a
    threshold is honoured, a root is scanned once, a floor is applied to every
    root), so the empty expectations file and the report stub are set up here
    once rather than re-spelled per test.
    """
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    commands = _stub_report(monkeypatch, [], **kwargs)
    assert gate._run(_args()) == 0
    return commands


def _root_relative(root: str, location: dict) -> dict:
    name = str(location["name"])
    if root != "." and name.startswith(root + "/"):
        name = name[len(root) + 1 :]
    return {**location, "name": name}


#: The scan roots this file's stubs understand, read from the gate so a root
#: added to the gate is a root these stubs model without a second edit.
_KNOWN_ROOTS = frozenset(
    root for roots in gate.PROFILE_PATHS.values() for root in roots
)


def _roots_of(command: list[str]) -> list[str]:
    """The scan roots an invocation was handed.

    Everything after the last flag, rather than ``command[-1]``: the cross-tree
    invocation is given every root as a positional, so the last one is not the
    only one and reading it that way would model that invocation as a `scripts`
    scan.
    """
    positionals = command[command.index("--fail-on-empty") + 1 :]
    return [argument for argument in positionals if argument in _KNOWN_ROOTS]


def _duplicate(
    first_path: str = "app/new.py",
    second_path: str = "app/other.py",
    fragment: str = "duplicate",
) -> dict:
    return {
        "firstFile": {"name": first_path, "start": 10, "end": 20},
        "secondFile": {"name": second_path, "start": 30, "end": 40},
        "fragment": fragment,
    }


def _valid_pair() -> dict:
    return {
        "id": "new-clone",
        "first": {"path": "app/new.py", "start": 10, "end": 20},
        "second": {"path": "app/other.py", "start": 30, "end": 40},
        "reason": "intentional",
        "guard": "tests/test_safety_invariants.py::test_classification_literal_covers_the_enum_exactly",
        "fragment_sha256": hashlib.sha256(b"duplicate").hexdigest(),
    }


_PROBE_BASENAME = "zz_probe_clone.py"
# The real module the probe is a copy of, by its path inside the mirrored tree.
PROBE_SOURCE = "app/crawl/ids.py"


def _mirrored_service_tree(tmp_path: Path, payload: str) -> Path:
    """A throwaway service tree carrying production's relative layout.

    `app/crawl/ids.py` is IN the mirror, because it is the other half of the
    production clone: the reported pair is the planted probe against the real
    module it was copied from, and a mirror holding only the probe would report
    nothing and pass for the wrong reason.

    `jscpd` matches each ignore glob against the path RELATIVE TO ITS OWN CWD,
    not against an absolute path, and that is what makes a tree under pytest's
    `tmp_path` a faithful stand-in: measured by scanning `app/` from a mirror
    rooted under `/tmp`, the `**/tmp/**` glob matched nothing while
    `**/app/crawl/migrations/**` excluded the migration clone and left the
    production clone reported. So this holds on a Linux checkout where
    `tmp_path` itself lives under `/tmp`, not only on a macOS tmp dir.
    """
    mirror = tmp_path / "service"
    # Two production files with identical content, plus the same content again
    # under the excluded migrations subtree.
    for relative in (PROBE_SOURCE, f"app/{_PROBE_BASENAME}", f"app/crawl/migrations/{_PROBE_BASENAME}"):
        planted = mirror / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(payload, encoding="utf-8")
    # One module per remaining root: enough for the gate's "analyzed 0 sources"
    # check, and a single file per root cannot form a pair of its own. It is the
    # real module rather than a stub because jscpd only counts a file as a
    # source once it holds enough tokens -- a file of trivial assignments is
    # scored as 0 sources however many lines it has.
    for tree in ("tests", "scripts"):
        filler = mirror / tree / "zz_tree_filler.py"
        filler.parent.mkdir(parents=True, exist_ok=True)
        filler.write_text(payload, encoding="utf-8")
    return mirror


_GATE_ARGV = [
    "gate",
    "all",
    "--min-lines",
    str(_CONFIGURED_MIN_LINES),
    "--min-tokens",
    str(_CONFIGURED_MIN_TOKENS),
    "--threshold",
    str(_CONFIGURED_THRESHOLD),
    "--fail-on-empty",
]


def _configure(monkeypatch, tmp_path, pair: dict, duplicate: list[dict]) -> list[list[str]]:
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, [pair])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", expectations)
    commands = _stub_report(monkeypatch, duplicate)
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)
    return commands


def _expect_nothing_sanctioned(monkeypatch, tmp_path, duplicates, **report) -> None:
    """An attestation file that sanctions nothing, plus a scan report we choose.

    Every gate failure that is NOT about a sanctioned pair needs exactly these
    two things, and was spelling both out at each site. ``_configure`` cannot
    stand in for it: that helper requires a declared pair, which is precisely
    what these tests are rejecting.
    """
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", expectations)
    _stub_report(monkeypatch, duplicates, **report)


def test_uniform_profile_shape() -> None:
    assert set(gate.PROFILE_PATHS) == {"all"}
    assert gate.MIN_SOURCES == {"all": 200}


def _cross_tree_duplicate() -> dict:
    """A fragment shared between production and a test, at the same floor.

    The shape this is about is a test harness re-spelling production's truth: it
    looks like one legitimate copy on each side, so a per-tree gate has nothing to
    object to even in principle.
    """
    return _duplicate("app/crawl/recursive.py", "tests/_recursive_harnesses.py")


def _sanctioned_cross_tree_pair() -> dict:
    """A declared cross-tree pair, pinned at the locations the duplicate reports.

    The locations are not incidental. ``_expected_tree`` reads the tree label off
    the two declared paths, so a declaration whose sides are in the same tree is
    keyed under that tree and will never be found by the cross-tree pass -- the
    one way to write an expectation here that reads as satisfied and pins
    nothing.
    """
    pair = _valid_pair()
    pair["first"] = {"path": "app/crawl/recursive.py", "start": 10, "end": 20}
    pair["second"] = {"path": "tests/_recursive_harnesses.py", "start": 30, "end": 40}
    return pair


def _stubbed_scan(duplicates: list[dict], sources: int = 400) -> dict[str, object]:
    """A cross-tree scan result, for the cases that replace the real one."""
    return {"statistics": {"total": {"sources": sources}}, "duplicates": duplicates}


def _without_cross_tree_scan(monkeypatch, duplicates: list[dict], sources: int = 400) -> None:
    monkeypatch.setattr(
        gate, "_cross_tree_scan", lambda floor, args: _stubbed_scan(duplicates, sources)
    )


def test_a_clone_spanning_two_trees_fails(monkeypatch, tmp_path, capsys) -> None:
    _expect_nothing_sanctioned(monkeypatch, tmp_path, [_cross_tree_duplicate()])

    assert gate._run(_args()) == 1
    error = capsys.readouterr().err
    assert "[cross]" in error
    # Both sides named, each with the root that actually holds it -- the gate has
    # to re-attach the root jscpd stripped, or a cross-tree report would be
    # unactionable.
    assert "app/crawl/recursive.py" in error
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


def test_a_pair_inside_one_tree_is_not_also_reported_as_cross_tree(
    monkeypatch, tmp_path, capsys
) -> None:
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
                "firstFile": {"name": "crawl/ids.py", "start": 10, "end": 20},
                "secondFile": {"name": "crawl/ids.py", "start": 30, "end": 40},
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


def test_jscpd_command_uses_service_venv(monkeypatch, tmp_path) -> None:
    commands = _run_against_empty_tree(monkeypatch, tmp_path)
    assert commands
    for command in commands:
        assert command[0] == str(gate.SERVICE_ROOT / ".venv" / "bin" / "jscpd")
        assert "--fail-on-empty" in command
        assert command[command.index("--threshold") + 1] == "0"
    # The floor each command carries is checked across all roots by
    # test_one_floor_is_applied_to_every_root; per-root floors no longer exist.


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


@pytest.mark.parametrize(
    ("field", "value"),
    [("reason", " "), ("guard", "tests/test_safety_invariants.py::test_does_not_exist")],
)
def test_expectations_reject_unusable_attestations(monkeypatch, tmp_path, field, value) -> None:
    pair = _valid_pair()
    pair[field] = value
    _configure(monkeypatch, tmp_path, pair, [_duplicate()])
    assert gate.main() == 1


def test_expectation_rejects_legacy_profile_shape(monkeypatch, tmp_path) -> None:
    pair = _valid_pair()
    pair["profiles"] = ["all"]
    _configure(monkeypatch, tmp_path, pair, [_duplicate()])
    assert gate.main() == 1


def test_expectation_rejects_changed_fragment(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, _valid_pair(), [_duplicate(fragment="changed")])
    assert gate.main() == 1


def test_valid_sanctioned_pair_passes(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, _valid_pair(), [_duplicate()])
    assert gate.main() == 0


def test_source_floor_fails_on_thin_scan(monkeypatch, tmp_path) -> None:
    _expect_nothing_sanctioned(monkeypatch, tmp_path, [], sources=60)

    assert gate._run(_args()) == 1


def test_a_tree_that_scanned_nothing_fails(monkeypatch, tmp_path, capsys) -> None:
    """A wrong scan root reports zero sources and no error, so it must fail."""
    _expect_nothing_sanctioned(
        monkeypatch,
        tmp_path,
        [],
        sources={"app": 0, "tests": 200, "scripts": 200},
    )

    assert gate._run(_args()) == 1
    assert "scan root is wrong" in capsys.readouterr().err


def _configure_scan_config(monkeypatch, tmp_path, ignore: list[str]) -> list[list[str]]:
    """Point the gate at a tampered .jscpd.json and record whether jscpd was ever run."""
    config = tmp_path / "jscpd.json"
    config.write_text(json.dumps({"ignore": ignore}), encoding="utf-8")
    monkeypatch.setattr(gate, "CONFIG_PATH", config)
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    monkeypatch.setattr(
        sys,
        "argv",
        _GATE_ARGV,
    )
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        raise AssertionError("jscpd must not run against an unvalidated scan config")

    monkeypatch.setattr(gate.subprocess, "run", run)
    return commands


def test_checked_in_scan_config_is_owned_by_the_gate() -> None:
    """`.jscpd.json` is enforcement configuration, so the gate validates it."""
    assert gate.CONFIG_PATH == gate.SERVICE_ROOT / ".jscpd.json"
    gate._validate_scan_config(gate.CONFIG_PATH)


def _at(first: tuple[str, int, int], second: tuple[str, int, int], fragment: str = "duplicate") -> dict:
    (first_path, first_start, first_end), (second_path, second_start, second_end) = first, second
    return {
        "firstFile": {"name": first_path, "start": first_start, "end": first_end},
        "secondFile": {"name": second_path, "start": second_start, "end": second_end},
        "fragment": fragment,
    }


DECLARED_FIRST = ("app/new.py", 10, 20)
DECLARED_SECOND = ("app/other.py", 30, 40)


def _shifted_by_one_line() -> dict:
    """The same content one line lower: a comment was inserted above the clone."""
    return _at(("app/new.py", 11, 21), ("app/other.py", 31, 41))


def test_a_sanctioned_clone_that_moved_is_drift_not_a_disappearance(monkeypatch, tmp_path, capsys) -> None:
    """Identity is the content, so moving a clone is recognised, not lost.

    Identity used to be (path, start, end) and `missing` was checked before
    `mismatched`, so inserting one comment line above a sanctioned clone
    reported it as missing and the way forward was to copy the new coordinates
    out of the error message into the expectation. The expectation's location
    was therefore a self-report the gate never checked.
    """
    _configure(monkeypatch, tmp_path, _valid_pair(), [_shifted_by_one_line()])
    assert gate.main() == 1
    reported = capsys.readouterr().err
    assert "missing" not in reported, f"a moved clone is not a missing clone:\n{reported}"
    assert "new-clone" in reported, f"the failure must name the expectation, not coordinates:\n{reported}"
    assert "drift" in reported.lower(), f"the failure must be reported as drift:\n{reported}"
    assert "app/new.py" in reported and "11" in reported, (
        f"the drift must state where it is now, so the expectation edit is deliberate:\n{reported}"
    )


def test_expectation_location_must_match_the_report_it_claims_to_pin(monkeypatch, tmp_path, capsys) -> None:
    """The declared bounds are checked, so they cannot quietly go stale."""
    pair = _valid_pair()
    pair["first"] = {"path": "app/new.py", "start": 999, "end": 1000}
    _configure(monkeypatch, tmp_path, pair, [_at(DECLARED_FIRST, DECLARED_SECOND)])
    assert gate.main() == 1
    reported = capsys.readouterr().err
    assert "drift" in reported.lower(), f"a wrong declared location must be drift:\n{reported}"
    assert "999" in reported, f"the failure must show the location the expectation claims:\n{reported}"


def _gate_says(monkeypatch, tmp_path, capsys, *pairs: dict) -> str:
    """Run the gate over a report holding one clone, with these expectations."""
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, list(pairs))
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", expectations)
    _stub_report(monkeypatch, [_duplicate()])
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)
    assert gate.main() == 1
    return capsys.readouterr().err


def _relocated(second: dict) -> dict:
    """The same expectation, moved elsewhere: new id, new content, new lines."""
    second["id"] = "other-clone"
    second["fragment_sha256"] = hashlib.sha256(b"a different clone").hexdigest()
    second["first"] = {"path": "app/third.py", "start": 50, "end": 60}
    return second


def test_two_expectations_cannot_claim_the_same_location(monkeypatch, tmp_path, capsys) -> None:
    """A location is claimed once or not at all.

    Two expectations pinning the same lines is not a duplicate the gate can
    resolve: at most one can ever be satisfied, and the loser used to be reported
    as a missing clone, which sends the reader after the duplication instead of
    after the expectations file.
    """
    second = _relocated(_valid_pair())
    second["first"] = _valid_pair()["first"]
    second["second"] = _valid_pair()["second"]
    reported = _gate_says(monkeypatch, tmp_path, capsys, _valid_pair(), second)
    assert "same location" in reported, f"a double-claimed location must be named as one:\n{reported}"
    assert "other-clone" in reported and "new-clone" in reported, f"both claimants must be named:\n{reported}"


def test_two_expectations_cannot_claim_the_same_fragment(monkeypatch, tmp_path, capsys) -> None:
    second = _relocated(_valid_pair())
    second["fragment_sha256"] = _valid_pair()["fragment_sha256"]
    reported = _gate_says(monkeypatch, tmp_path, capsys, _valid_pair(), second)
    assert "same fragment" in reported, f"two expectations on one fragment must be refused:\n{reported}"


def test_two_expectations_cannot_share_an_id(monkeypatch, tmp_path, capsys) -> None:
    second = _relocated(_valid_pair())
    second["id"] = _valid_pair()["id"]
    reported = _gate_says(monkeypatch, tmp_path, capsys, _valid_pair(), second)
    assert "duplicate jscpd expectation id" in reported, f"a shared id must be refused:\n{reported}"


def test_every_root_is_scanned_by_its_own_single_root_invocation() -> None:
    """One root per invocation, because jscpd's names are root-relative.

    `name` is relative to the scan root, so an invocation given several roots
    cannot tell production from test: `app/crawl/x.py` and `tests/crawl/x.py`
    both arrive as `crawl/x.py`, and a clone spanning the two is reported
    against a path that names neither tree. A tree may hold several roots, but
    each of them is scanned alone, which is what makes this gate's own report
    able to say which tree it is talking about.
    """
    roots = [root for tree_roots in gate.TREE_ROOTS.values() for root in tree_roots]
    assert len(set(roots)) == len(roots), f"a root scanned twice is ambiguous: {roots}"
    assert gate.PROFILE_PATHS["all"] == tuple(roots)
    assert "." not in roots, (
        "a whole-tree scan cannot be partitioned afterwards: jscpd's fragment "
        "boundaries move with the floor, so filtering one loose scan down to a "
        "tighter floor is not the set of pairs that floor would report"
    )


def test_each_root_gets_exactly_one_invocation(monkeypatch, tmp_path) -> None:
    """One invocation per root, plus exactly one that spans all of them.

    The spanning invocation is not a second look at the same thing. Each root's
    own invocation cannot report a pair whose two sides are in different roots,
    because jscpd only compares files it resolved from the paths it was handed --
    so a fragment shared between `app/` and `tests/` is invisible to every
    per-root scan, and the class is not small. It has to be asked for separately,
    and asked for exactly once.
    """
    commands = _run_against_empty_tree(monkeypatch, tmp_path)
    spanning = [command for command in commands if len(_roots_of(command)) > 1]
    assert len(spanning) == 1, f"expected one cross-root invocation, got {len(spanning)}"
    assert _roots_of(spanning[0]) == list(gate.PROFILE_PATHS["all"])
    scanned = [_roots_of(command)[0] for command in commands if len(_roots_of(command)) == 1]
    assert scanned == list(gate.PROFILE_PATHS["all"]), f"one invocation per root, got {scanned}"


def test_one_floor_is_applied_to_every_root(monkeypatch, tmp_path) -> None:
    """No root is scored at a looser bound than another.

    Separate floors were the fix for a flat floor hiding production duplication
    behind a number picked for test scaffolding. They became unnecessary once
    this tree reported zero pairs in every tree at one number, and a second
    number is a second thing to ratchet -- so there is one floor now, and the
    check is that every invocation carries it. A root added later, or a floor
    edited in one place only, is what this catches.
    """
    commands = _run_against_empty_tree(monkeypatch, tmp_path)
    assert len(commands) > 1, "expected more than one root, so the check has something to compare"
    carried = {
        (command[command.index("--min-lines") + 1], command[command.index("--min-tokens") + 1])
        for command in commands
    }
    assert carried == {
        (str(_CONFIGURED_MIN_LINES), str(_CONFIGURED_MIN_TOKENS))
    }, f"roots scored at differing floors: {sorted(carried)}"


def test_the_same_content_in_two_trees_is_two_findings_not_one(monkeypatch, tmp_path, capsys) -> None:
    """Keyed by content AND tree, so the two cannot collapse into one pair.

    This is the defect a single whole-tree scan at a single floor carried: the
    same fragment appearing in `app/` and in `tests/` is one content hash, and
    one content hash cannot say which tree it belongs to or which floor it
    should be judged by. Test boilerplate must not become cover for production
    code, so each is reported against the tree that actually holds it.
    """
    _expect_nothing_sanctioned(
        monkeypatch,
        tmp_path,
        [
            _duplicate("app/one.py", "app/two.py", fragment="shared"),
            _duplicate("tests/one.py", "tests/two.py", fragment="shared"),
        ],
    )
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)

    assert gate.main() == 1
    reported = capsys.readouterr().err
    assert reported.count("unsanctioned clone") == 2, (
        f"one pair per tree, not one collapsed pair:\n{reported}"
    )
    assert "[production]" in reported and "[test]" in reported, f"each pair must name its tree:\n{reported}"


def test_migrations_exclusion_is_a_directory_glob_so_new_migrations_are_covered() -> None:
    """The exclusion is a subtree, not a list of today's migration files.

    `0001_crawl_jobs_initial.py` and `0001_results_initial.py` are flagged as an
    8L/31T clone, and that is the correct shape: migrations are replayed by name
    and two files creating parallel tables is what they are for. A per-file glob
    would need editing every time a migration lands, which is the failure mode
    this whole config is now gated against.
    """
    assert "**/app/crawl/migrations/**" in gate.SANCTIONED_IGNORE_GLOBS
    assert "**/app/crawl/migrations/**" in json.loads(gate.CONFIG_PATH.read_text(encoding="utf-8"))["ignore"]


def test_migrations_clones_are_not_reported_but_production_clones_are(monkeypatch, tmp_path, capsys) -> None:
    """One real scan of a mirrored tree, two planted clones: the migrations copy
    is invisible, the production copy is not. Both plants are files this config
    never names, so this also proves a migration added tomorrow is covered by
    the glob.

    The tree is mirrored rather than borrowed, and that is not a convenience.
    This test used to plant the pair at fixed paths in the LIVE `app/` tree and
    unlink them in a `finally`, which made the production tree a directory that
    moves while a suite runs: every concurrent walk of `app/` became a
    list-then-read race, and with 6 concurrent copies of this file against 6
    concurrent copies of tests/test_safety_invariants.py, 10 tests died with
    `FileNotFoundError: .../app/crawl/migrations/zz_probe_clone.py` -- the
    tree-wide chokepoint detectors among them, which then reported nothing for
    the whole run while looking green. The glob under test is about path SHAPE,
    so a mirror with the same relative layout is what this needs anyway.
    """
    payload = (gate.SERVICE_ROOT / PROBE_SOURCE).read_text(encoding="utf-8")
    assert len(payload.splitlines()) >= 8, "the probe needs a clone big enough to clear the floor"

    mirror = _mirrored_service_tree(tmp_path, payload)
    _write_expectations(mirror / "expectations.json", [])
    # The real `.jscpd.json` is NOT redirected: the ignore list is the thing
    # under test. The venv is a property of the machine, not of the scanned
    # tree, so the mirror borrows the real one and the jscpd invocation stays
    # the same binary. TREE_ROOTS keeps its production values; only the source
    # floor moves, because this tree is four files instead of three hundred.
    (mirror / ".venv").symlink_to(gate.SERVICE_ROOT / ".venv", target_is_directory=True)
    monkeypatch.setattr(gate, "SERVICE_ROOT", mirror)
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", mirror / "expectations.json")
    monkeypatch.setattr(gate, "MIN_SOURCES", {"all": 1})

    assert gate._run(_args()) == 1

    reported = capsys.readouterr().err
    assert "app/zz_probe_clone.py" in reported, f"a production clone must be reported:\n{reported}"
    assert "migrations/zz_probe_clone.py" not in reported, (
        f"a migration clone must be excluded by the glob, not reported:\n{reported}"
    )


@pytest.mark.parametrize("drift", ["added-glob", "removed-glob", "whole-tree-glob"])
def test_unowned_ignore_list_fails_before_any_scan(monkeypatch, tmp_path, drift) -> None:
    sanctioned = gate.SANCTIONED_IGNORE_GLOBS
    if drift == "added-glob":
        ignore = [*sanctioned, "**/zz_probe_clone.py"]
    elif drift == "removed-glob":
        ignore = sanctioned[:-1]
    else:
        ignore = [*sanctioned, "**"]

    commands = _configure_scan_config(monkeypatch, tmp_path, ignore)
    assert gate.main() == 1
    assert commands == []


@pytest.mark.parametrize("glob", ["**/*.py", "app/*.py", "**/zz_probe_clone.py", "*", "**"])
def test_python_source_globs_are_never_a_sanctioned_exception(glob) -> None:
    """The ignore list exists to skip caches and vendored trees, never source."""
    with pytest.raises(ValueError, match="Python source|whole tree"):
        gate._reject_source_masking([glob])


@pytest.mark.parametrize(
    "unsanctioned",
    [{"maxLines": 500}, {"skipLocal": True}],
    ids=["maxLines", "skipLocal"],
)
def test_jscpd_json_rejects_unknown_keys(tmp_path, unsanctioned) -> None:
    """Bites: only `ignore` is allowed in .jscpd.json.

    The measured masking, and the reason the check runs before any scan, are
    recorded on ``gate._validate_scan_config`` -- restating them here is a
    second copy to go stale. This test only pins the rejection, and takes its
    cases as a table because the one thing every unsanctioned key has in common
    is being unsanctioned -- which is the rule, spelled once.
    """
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS, **unsanctioned}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        gate._validate_scan_config(config)


def test_jscpd_json_accepts_only_ignore(tmp_path) -> None:
    """A config with only `ignore` passes."""
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS}),
        encoding="utf-8",
    )
    gate._validate_scan_config(config)


def test_the_test_suite_reads_the_floor_the_hook_runs() -> None:
    """The argv these tests gate on is the hook's own floor, not a copy of it.

    Four spellings of 4/39 used to exist -- the hook entry, the hook's name, this
    module's ``_args_for`` default, and this module's ``_GATE_ARGV`` -- with
    nothing comparing them. The failure that shape hides is not a wrong number, it
    is a green suite: every test here would keep passing against 4/39 while
    commits were gated at whatever the config said, so the suite would vouch for a
    floor that was no longer enforced. Asserting the argv against the config is
    what turns the copy into a derived value.
    """
    argv = _GATE_ARGV
    assert (argv[argv.index("--min-lines") + 1], argv[argv.index("--min-tokens") + 1]) == (
        str(_CONFIGURED_MIN_LINES),
        str(_CONFIGURED_MIN_TOKENS),
    )
    assert _args_for().min_tokens == _CONFIGURED_MIN_TOKENS


def test_a_laxer_floor_is_refused_because_it_cannot_report_the_truth() -> None:
    """A run handed a looser floor than the hook enforces fails instead of passing.

    A duplication gate's number is a command-line argument, so "run it at 8/40" is
    one flag away from the enforced value and returns a genuinely clean result for
    the floor that was asked for. Reporting that as a pass is the whole hazard: the
    output is true and the conclusion is wrong. Only laxer is refused -- scanning
    stricter than the hook is how a candidate floor gets measured before adoption.
    """
    laxer = _args_for(
        min_lines=_CONFIGURED_MIN_LINES + 1,
        min_tokens=_CONFIGURED_MIN_TOKENS + 1,
    )
    with pytest.raises(ValueError, match="laxer than the enforced floor"):
        gate._require_configured_floor(laxer)


def test_the_enforced_floor_and_a_stricter_one_are_both_accepted() -> None:
    """The floor itself, and anything below it, are legitimate runs."""
    gate._require_configured_floor(_args_for())
    gate._require_configured_floor(
        _args_for(min_lines=_CONFIGURED_MIN_LINES, min_tokens=_CONFIGURED_MIN_TOKENS - 1)
    )


def test_a_floor_the_hook_does_not_state_is_unreadable_rather_than_defaulted(monkeypatch, tmp_path) -> None:
    """Bites: a hook entry missing a threshold raises instead of reading as one.

    The failure this guards is a silent one. If an unreadable floor fell back to a
    default, the gate would go on comparing clones against a number the config
    stopped stating, and the guard refusing lax runs would refuse them against that
    same phantom value -- two checks agreeing on a floor nobody chose.

    The stripped copy is written to ``tmp_path`` and the module is pointed at it,
    because the alternative -- editing the real config to observe that it becomes
    unreadable -- leaves the repository one failed test away from a gate that
    cannot name its own floor.
    """
    stripped = (tmp_path / "config.yaml").write_text(
        gate.PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8").replace(
            f"--min-tokens {_CONFIGURED_MIN_TOKENS}",
            "",
        ),
        encoding="utf-8",
    )
    assert "--min-tokens" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert stripped
    monkeypatch.setattr(gate, "PRECOMMIT_CONFIG_PATH", tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="declares no --min-tokens"):
        gate.configured_floor()


def test_every_sanctioned_pair_names_a_guard_that_exists_and_pins_its_fragment() -> None:
    """The baseline is load-bearing: a sanction is a guard, not a comment.

    A sanction is a standing licence to duplicate, so the baseline is empty and
    that is the claim being asserted. The two pairs it held were the fetcher
    contract and the selector pair, each of which existed only because a
    machine-checked guard required two spellings of one contract; both were
    consolidated, so the licence is no longer needed and is withdrawn rather than
    left in place for the next duplicate to hide behind.

    The loop is the other half and is what fires if a pair is ever sanctioned
    again: a sanction whose guard test was renamed or deleted would leave the copy
    unchecked and the gate green, which is the shape of drift this whole program
    is closing. The hash is what makes a baseline entry a claim about today's
    code -- change either side of a pair and the recorded hash stops matching, so
    the entry fails as unsanctioned rather than continuing to vouch for a fragment
    that no longer exists.
    """
    sanctioned = [pair["id"] for pair in gate._expected_pairs()]
    assert not sanctioned, (
        f"duplicate pairs are sanctioned again: {sanctioned}. A sanction is a standing licence "
        "to duplicate; if a guard genuinely requires two spellings, that is a chokepoint to "
        "build, not a pair to allow -- see app/crawl/extraction.py Selectors and "
        "app/models.py HttpFetchContract for the two this baseline used to hold."
    )
    for pair in gate._expected_pairs():
        guard = pair["guard"]
        path, _, node = guard.partition("::")
        assert node in gate._test_names(gate.SERVICE_ROOT / path), f"{pair['id']} names a guard that is not there"
        assert len(pair["fragment_sha256"]) == 64, f"{pair['id']} has no content pin"
