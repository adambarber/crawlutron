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


def _args_for(
    threshold: float = 0,
    *,
    min_lines: int = 8,
    min_tokens: int = 40,
    app_min_lines: int = 7,
    app_min_tokens: int = 35,
) -> argparse.Namespace:
    return argparse.Namespace(
        profile="all",
        min_lines=min_lines,
        min_tokens=min_tokens,
        app_min_lines=app_min_lines,
        app_min_tokens=app_min_tokens,
        threshold=threshold,
        fail_on_empty=True,
    )


def _write_expectations(path: Path, pairs: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "pairs": pairs}), encoding="utf-8")


def _stub_report(
    monkeypatch,
    duplicates: list[dict],
    percentage: float = 0,
    sources: int | dict[str, int] = 200,
) -> list[list[str]]:
    commands: list[list[str]] = []

    def _inside(root: str, name: str) -> bool:
        return root == "." or name == root or name.startswith(root + "/")

    def run(command, **_kwargs):
        commands.append(command)
        root = command[-1]
        # jscpd reports `name` relative to the scan root, so a stub that returned
        # every duplicate for every invocation would be modelling a tool that
        # does not exist. Only pairs inside the scanned root are reported, and
        # their names come back root-relative.
        scoped = [
            {
                **duplicate,
                "firstFile": _root_relative(root, duplicate["firstFile"]),
                "secondFile": _root_relative(root, duplicate["secondFile"]),
            }
            for duplicate in duplicates
            if _inside(root, duplicate["firstFile"]["name"]) and _inside(root, duplicate["secondFile"]["name"])
        ]
        found = sources[root] if isinstance(sources, dict) else sources
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
    return next(command for command in commands if command[-1] == root)


def _root_relative(root: str, location: dict) -> dict:
    name = str(location["name"])
    if root != "." and name.startswith(root + "/"):
        name = name[len(root) + 1 :]
    return {**location, "name": name}


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
        "guard": "tests/test_safety_invariants.py::test_crawl_error_members_derive_from_classification",
        "fragment_sha256": hashlib.sha256(b"duplicate").hexdigest(),
    }


_GATE_ARGV = [
    "gate",
    "all",
    "--min-lines",
    "8",
    "--min-tokens",
    "40",
    "--app-min-lines",
    "7",
    "--app-min-tokens",
    "35",
    "--threshold",
    "0",
    "--fail-on-empty",
]


def _configure(monkeypatch, tmp_path, pair: dict, duplicate: list[dict]) -> list[list[str]]:
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, [pair])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", expectations)
    commands = _stub_report(monkeypatch, duplicate)
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)
    return commands


def test_uniform_profile_shape() -> None:
    assert set(gate.PROFILE_PATHS) == {"all"}
    assert gate.MIN_SOURCES == {"all": 200}


def test_jscpd_command_uses_service_venv(monkeypatch, tmp_path) -> None:
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    commands = _stub_report(monkeypatch, [])

    assert gate._run(_args()) == 0
    assert commands
    for command in commands:
        assert command[0] == str(gate.SERVICE_ROOT / ".venv" / "bin" / "jscpd")
        assert "--fail-on-empty" in command
        assert command[command.index("--threshold") + 1] == "0"
    # Each tree is scanned at its own floor, so the floor a command carries is
    # the one for the root it scans.
    production = _command_for(commands, "app")
    assert production[production.index("--min-lines") + 1] == "7"
    assert production[production.index("--min-tokens") + 1] == "35"
    support = _command_for(commands, "tests")
    assert support[support.index("--min-lines") + 1] == "8"
    assert support[support.index("--min-tokens") + 1] == "40"
    assert _command_for(commands, "scripts")[_command_for(commands, "scripts").index("--min-lines") + 1] == "8"


@pytest.mark.parametrize("directory", ("app", "tests", "scripts"))
def test_undeclared_clone_in_each_service_tree_fails(monkeypatch, tmp_path, directory):
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    _stub_report(
        monkeypatch,
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
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    _stub_report(monkeypatch, [], sources=60)

    assert gate._run(_args()) == 1


def test_a_tree_that_scanned_nothing_fails(monkeypatch, tmp_path, capsys) -> None:
    """A wrong scan root reports zero sources and no error, so it must fail."""
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    _stub_report(monkeypatch, [], sources={"app": 0, "tests": 200, "scripts": 200})

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
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    commands = _stub_report(monkeypatch, [])

    assert gate._run(_args()) == 0
    scanned = [command[-1] for command in commands]
    assert scanned == list(gate.PROFILE_PATHS["all"]), f"one invocation per root, got {scanned}"


def test_the_production_floor_is_tighter_than_the_test_floor() -> None:
    """One flat floor cannot serve both trees.

    Measured on this tree at 8 lines / 40 tokens the whole service reported
    exactly one pair, both sides in `app/`: production duplication was hidden
    behind a floor chosen for test scaffolding. At 5 / 25 the same tree reports
    169 pairs, 46 of them production. The floors are separate numbers so the
    production tree can be scored on its own evidence.
    """
    production = _args_for(min_lines=8, min_tokens=40, app_min_lines=7, app_min_tokens=35)
    floors = gate._floors(production)
    assert floors["production"] == (7, 35)
    assert floors["test"] == (8, 40)
    assert floors["production"] < floors["test"], "the production floor must be the tighter one"


def test_the_same_content_in_two_trees_is_two_findings_not_one(monkeypatch, tmp_path, capsys) -> None:
    """Keyed by content AND tree, so the two cannot collapse into one pair.

    This is the defect a single whole-tree scan at a single floor carried: the
    same fragment appearing in `app/` and in `tests/` is one content hash, and
    one content hash cannot say which tree it belongs to or which floor it
    should be judged by. Test boilerplate must not become cover for production
    code, so each is reported against the tree that actually holds it.
    """
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    _stub_report(
        monkeypatch,
        [
            _duplicate("app/one.py", "app/two.py", fragment="shared"),
            _duplicate("tests/one.py", "tests/two.py", fragment="shared"),
        ],
    )
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)

    assert gate.main() == 1
    reported = capsys.readouterr().err
    assert reported.count("unsanctioned clone") == 2, f"one pair per tree, not one collapsed pair:\n{reported}"
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


def test_migrations_clones_are_not_reported_but_production_clones_are(capsys) -> None:
    """One real scan, two planted clones: the migrations copy is invisible, the
    production copy is not. Both plants are files this config never names, so
    this also proves a migration added tomorrow is covered by the glob."""
    probe = gate.SERVICE_ROOT / "app" / "crawl" / "ids.py"
    in_migrations = gate.SERVICE_ROOT / "app" / "crawl" / "migrations" / "zz_probe_clone.py"
    in_production = gate.SERVICE_ROOT / "app" / "zz_probe_clone.py"
    payload = probe.read_text(encoding="utf-8")
    assert len(payload.splitlines()) >= 8, "the probe needs a clone big enough to clear the floor"
    in_migrations.write_text(payload, encoding="utf-8")
    in_production.write_text(payload, encoding="utf-8")
    try:
        assert gate._run(_args()) == 1
    finally:
        in_migrations.unlink()
        in_production.unlink()

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
