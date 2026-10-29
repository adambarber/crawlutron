"""One floor per root; thin scans, lax floors and unowned configs fail."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from scripts import _jscpd_pair_resolution as res
from scripts import jscpd_gate as gate
from tests._jscpd_gate_helpers import (
    _CONFIGURED_MIN_LINES,
    _CONFIGURED_MIN_TOKENS,
    _GATE_ARGV,
    _args,
    _args_for,
    _duplicate,
    _roots_of,
    _run_against_empty_tree,
    _stub_report,
    scan_run_stub,
    stub_resolver_probe,
)


def test_the_test_suite_reads_the_floor_the_hook_runs() -> None:
    """The argv these tests gate on is the hook's own floor, not a copy of it."""
    argv = _GATE_ARGV
    assert (argv[argv.index("--min-lines") + 1], argv[argv.index("--min-tokens") + 1]) == (
        str(_CONFIGURED_MIN_LINES),
        str(_CONFIGURED_MIN_TOKENS),
    )
    assert _args_for().min_tokens == _CONFIGURED_MIN_TOKENS


def test_the_jscpd_gate_helpers_live_in_the_helper_namespace() -> None:
    """A test_* file with zero tests is a lie; the gate helpers are _-prefixed.

    This suite reads its fixtures off ``tests/_jscpd_gate_helpers.py`` (above);
    that file must itself define no collectible test.
    """
    helper = Path(__file__).resolve().parent / "_jscpd_gate_helpers.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    test_defs = [
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
    ]
    assert test_defs == [], f"a helper file must not also define test_* defs: {test_defs}"


def test_a_laxer_floor_is_refused_because_it_cannot_report_the_truth() -> None:
    """A run handed a looser floor than the hook enforces fails instead of passing."""
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
    """Bites: a hook entry missing a threshold raises instead of reading as one."""
    stripped = (tmp_path / "config.yaml").write_text(
        gate.PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8").replace(
            f"--min-tokens {_CONFIGURED_MIN_TOKENS}",
            "",
        ),
        encoding="utf-8",
    )
    assert "--min-tokens" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert stripped
    monkeypatch.setattr(res, "PRECOMMIT_CONFIG_PATH", tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="declares no --min-tokens"):
        gate.configured_floor()


def test_fail_on_empty_is_no_longer_a_required_cli_flag(monkeypatch, capsys) -> None:
    """Bites: the gate runs with argv carrying no --fail-on-empty at all.

    The flag was required and read by nothing, so it was deleted as dead gate
    machinery; the gate still fails closed.
    """
    commands = _stub_report(monkeypatch, [])
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)
    assert "--fail-on-empty" not in _GATE_ARGV
    assert gate.main() == 0
    assert commands, "the gate really ran, it did not no-op"
    for command in commands:
        assert "--fail-on-empty" in command, "a fail-closed gate keeps the flag on the jscpd command"


def test_source_floor_fails_on_thin_scan(monkeypatch) -> None:
    _stub_report(monkeypatch, [], sources=60)

    assert gate._run(_args()) == 1


def test_a_tree_that_scanned_nothing_fails(monkeypatch, capsys) -> None:
    """A wrong scan root reports zero sources and no error, so it must fail."""
    _stub_report(
        monkeypatch,
        [],
        sources={"crawlutron": 0, "tests": 200, "scripts": 200},
    )

    assert gate._run(_args()) == 1
    assert "scan root is wrong" in capsys.readouterr().err


def _configure_scan_config(monkeypatch, tmp_path, ignore: list[str]) -> list[list[str]]:
    """Point the gate at a tampered .jscpd.json and record whether jscpd was ever run."""
    config = tmp_path / "jscpd.json"
    config.write_text(json.dumps({"ignore": ignore}), encoding="utf-8")
    monkeypatch.setattr(res, "CONFIG_PATH", config)
    monkeypatch.setattr(
        sys,
        "argv",
        _GATE_ARGV,
    )
    commands: list[list[str]] = []

    def scan(command) -> object:
        raise AssertionError("jscpd must not run against an unvalidated scan config")

    stub_resolver_probe(monkeypatch)
    scan_run_stub(monkeypatch, commands, scan)
    return commands


@pytest.mark.parametrize("drift", ["added-glob", "removed-glob", "whole-tree-glob"])
def test_unowned_ignore_list_fails_before_any_scan(monkeypatch, tmp_path, drift) -> None:
    sanctioned = gate.SANCTIONED_IGNORE_GLOBS
    if drift == "added-glob":
        ignore = [*sanctioned, "**/zz_probe_clone.py"]
    elif drift == "removed-glob":
        ignore = list(sanctioned[:-1])
    else:
        ignore = [*sanctioned, "**"]

    commands = _configure_scan_config(monkeypatch, tmp_path, ignore)
    assert gate.main() == 1
    assert commands == []


def test_checked_in_scan_config_is_owned_by_the_gate() -> None:
    """`.jscpd.json` is enforcement configuration, so the gate validates it."""
    assert gate.CONFIG_PATH == gate.SERVICE_ROOT / ".jscpd.json"
    res._validate_scan_config(gate.CONFIG_PATH)


@pytest.mark.parametrize("glob", ["**/*.py", "crawlutron/*.py", "**/zz_probe_clone.py", "*", "**"])
def test_python_source_globs_are_never_a_sanctioned_exception(glob) -> None:
    """The ignore list exists to skip caches and vendored trees, never source."""
    with pytest.raises(ValueError, match="Python source|whole tree"):
        res._reject_source_masking([glob])


@pytest.mark.parametrize(
    "unsanctioned",
    [{"maxLines": 500}, {"skipLocal": True}],
    ids=["maxLines", "skipLocal"],
)
def test_jscpd_json_rejects_unknown_keys(tmp_path, unsanctioned) -> None:
    """Bites: only ``ignore`` is allowed in .jscpd.json."""
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS, **unsanctioned}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        res._validate_scan_config(config)


def test_jscpd_json_accepts_only_ignore(tmp_path) -> None:
    """A config with only `ignore` passes."""
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS}),
        encoding="utf-8",
    )
    res._validate_scan_config(config)


def test_every_root_is_scanned_by_its_own_single_root_invocation() -> None:
    """One root per invocation because jscpd's `name` is root-relative: fed several
    roots it cannot tell production from test (`crawlutron/api/x.py` vs `tests/api/x.py`
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


def test_each_root_gets_exactly_one_invocation(monkeypatch) -> None:
    """The spanning invocation is not a second look: a per-root scan cannot see a
    pair whose sides are in different roots (jscpd only compares what it resolved),
    so such a fragment is invisible per-root and must be asked for once.
    """
    commands = _run_against_empty_tree(monkeypatch)
    spanning = [command for command in commands if len(_roots_of(command)) > 1]
    assert len(spanning) == 1, f"expected one cross-root invocation, got {len(spanning)}"
    assert _roots_of(spanning[0]) == list(gate.PROFILE_PATHS["all"])
    scanned = [_roots_of(command)[0] for command in commands if len(_roots_of(command)) == 1]
    assert scanned == list(gate.PROFILE_PATHS["all"]), f"one invocation per root, got {scanned}"


def test_one_floor_is_applied_to_every_root(monkeypatch) -> None:
    """No root is scored looser than another. Separate floors once hid production
    duplication behind test-scaffolding numbers; with zero pairs at one number they
    are unnecessary, so one floor, carried by every invocation.
    """
    commands = _run_against_empty_tree(monkeypatch)
    assert len(commands) > 1, "expected more than one root, so the check has something to compare"
    carried = {
        (command[command.index("--min-lines") + 1], command[command.index("--min-tokens") + 1])
        for command in commands
    }
    assert carried == {(str(_CONFIGURED_MIN_LINES), str(_CONFIGURED_MIN_TOKENS))}, (
        f"roots scored at differing floors: {sorted(carried)}"
    )


def test_the_same_content_in_two_trees_is_two_findings_not_one(monkeypatch, capsys) -> None:
    """Keyed by content AND tree so the two cannot collapse: one whole-tree scan sees
    a crawlutron/ and tests/ fragment as one content hash, which names neither tree nor
    floor -- test boilerplate must not cover production code.
    """
    _stub_report(
        monkeypatch,
        [
            _duplicate("crawlutron/one.py", "crawlutron/two.py", fragment="shared"),
            _duplicate("tests/one.py", "tests/two.py", fragment="shared"),
        ],
    )
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)

    assert gate.main() == 1
    reported = capsys.readouterr().err
    assert reported.count("[production] duplicate:") == 1, f"one production finding, not none:\n{reported}"
    assert reported.count("[test] duplicate:") == 1, f"one test finding, not none:\n{reported}"
