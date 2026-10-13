"""One floor per root; thin scans and lax floors fail."""

import hashlib
import json
import sys

from scripts import jscpd_gate as gate
from tests.test_jscpd_gate import (
    _CONFIGURED_MIN_LINES,
    _CONFIGURED_MIN_TOKENS,
    _GATE_ARGV,
    _args,
    _configure,
    _duplicate,
    _expect_nothing_sanctioned,
    _roots_of,
    _run_against_empty_tree,
    _stub_report,
    _valid_pair,
    _write_expectations,
)


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
    cannot tell production from test: `app/api/x.py` and `tests/api/x.py`
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
    assert carried == {(str(_CONFIGURED_MIN_LINES), str(_CONFIGURED_MIN_TOKENS))}, (
        f"roots scored at differing floors: {sorted(carried)}"
    )


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
