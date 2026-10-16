"""One floor per root; thin scans and lax floors fail."""

from __future__ import annotations

import hashlib
import json
import sys

from scripts import _jscpd_pair_resolution as res
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
    monkeypatch.setattr(res, "CONFIG_PATH", config)
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(res, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    monkeypatch.setattr(
        sys,
        "argv",
        _GATE_ARGV,
    )
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        raise AssertionError("jscpd must not run against an unvalidated scan config")

    monkeypatch.setattr(res.subprocess, "run", run)
    return commands


def test_checked_in_scan_config_is_owned_by_the_gate() -> None:
    """`.jscpd.json` is enforcement configuration, so the gate validates it."""
    assert gate.CONFIG_PATH == gate.SERVICE_ROOT / ".jscpd.json"
    res._validate_scan_config(gate.CONFIG_PATH)


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
    """Identity is content, so a moved clone is drift, not loss: identity used to be
    (path, start, end) and `missing` was checked first, so a comment shove reported
    it missing and the expectation became a self-report the gate never checked.
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
    monkeypatch.setattr(res, "EXPECTATIONS_PATH", expectations)
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
    """A location is claimed once or not at all. Two expectations on the same lines
    cannot both be satisfied, and the loser used to be reported as a missing clone
    -- sending the reader after the duplication, not the expectations file.
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
    """One root per invocation because jscpd's `name` is root-relative: fed several
    roots it cannot tell production from test (`app/api/x.py` vs `tests/api/x.py`
    both arrive as `crawl/x.py`), so a cross-tree clone names neither.
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
    """The spanning invocation is not a second look: a per-root scan cannot see a
    pair whose sides are in different roots (jscpd only compares what it resolved),
    so such a fragment is invisible per-root and must be asked for once.
    """
    commands = _run_against_empty_tree(monkeypatch, tmp_path)
    spanning = [command for command in commands if len(_roots_of(command)) > 1]
    assert len(spanning) == 1, f"expected one cross-root invocation, got {len(spanning)}"
    assert _roots_of(spanning[0]) == list(gate.PROFILE_PATHS["all"])
    scanned = [_roots_of(command)[0] for command in commands if len(_roots_of(command)) == 1]
    assert scanned == list(gate.PROFILE_PATHS["all"]), f"one invocation per root, got {scanned}"


def test_one_floor_is_applied_to_every_root(monkeypatch, tmp_path) -> None:
    """No root is scored looser than another. Separate floors once hid production
    duplication behind test-scaffolding numbers; with zero pairs at one number they
    are unnecessary, so one floor, carried by every invocation.
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
    """Keyed by content AND tree so the two cannot collapse: one whole-tree scan sees
    an app/ and tests/ fragment as one content hash, which names neither tree nor
    floor -- test boilerplate must not cover production code.
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
