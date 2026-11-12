"""The pre-commit chain admits a commit only through the full randomized battery.

The old chain ran tree-shape gates plus a pytest subset, so a behavioral
regression could land hooks-green. Each test names the mutant it kills.
"""

from __future__ import annotations

import re
import shlex

import pytest

from scripts import gate_fanout
from scripts.gate_fanout import ENGINE_MODULE, _gate_argv
from tests._ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_TRIGGER_PATTERN,
    _entry,
    _field,
    _hook_blocks,
)

BATTERY_HOOK_ID = "battery"


def _config() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def _battery(hooks: list[tuple[str, dict[str, object]]]) -> dict[str, object]:
    found = [block for hook_id, block in hooks if hook_id == BATTERY_HOOK_ID]
    assert len(found) == 1, f"exactly one {BATTERY_HOOK_ID} hook required, found {len(found)}"
    return found[0]


def test_the_battery_hook_is_declared_at_pre_commit() -> None:
    """Kills the delete-the-battery-hook mutant: remove the hook and the chain
    is tree-shape-only again, so a behavioral regression lands hooks-green."""
    battery = _battery(_hook_blocks(_config()))
    assert battery["stages"] == ["pre-commit"]
    assert _field(battery, "files") == ENFORCEMENT_TRIGGER_PATTERN
    assert ENGINE_MODULE in _entry(battery)


def test_one_pytest_child_runs_at_pre_commit_and_it_is_the_battery() -> None:
    """Kills the resurrect-a-subset-pytest-hook mutant: any second pre-commit
    pytest child (the retired invariants-subset shape) reds the census, as does
    a battery missing from the whole-chain pytest surface."""
    hooks = _hook_blocks(_config())
    at_commit = [
        hook_id
        for hook_id, block in hooks
        if "pre-commit" in str(_field(block, "stages")) and "-m pytest" in _entry(block)
    ]
    assert at_commit == [BATTERY_HOOK_ID], f"pre-commit pytest children: {at_commit}"
    everywhere = {hook_id for hook_id, block in hooks if "-m pytest" in _entry(block)}
    assert everywhere == {BATTERY_HOOK_ID, "coverage"}, (
        f"unexpected pytest surfaces in the hook chain: {sorted(everywhere)}"
    )


def test_the_battery_child_runs_the_whole_suite_with_default_addopts() -> None:
    """Kills the narrowed-battery mutants: a path/file argument, an `-o
    addopts=` override, an ignore/deselect/-k/-m filter, or a `-p` plugin
    switch each red — the child must be the default whole suite."""
    battery = _battery(_hook_blocks(_config()))
    argv = shlex.split(_entry(battery).split(" -- ", 1)[1])
    assert argv[:3] == [".venv/bin/python", "-m", "pytest"]
    rest = argv[3:]
    assert rest == ["-q"], f"battery child carries unexpected switches: {rest}"
    for token in rest:
        assert not re.match(r"^(tests|crawlutron|scripts)(/|$)", token), (
            f"path argument {token!r} narrows the battery"
        )
        assert not token.endswith(".py"), f"file argument {token!r} narrows the battery"


def test_the_fanout_discovers_every_precommit_pytest_child() -> None:
    """Kills the un-wired-battery mutant: a battery hook the fanout's parser
    misses (wrong stage, entry without the engine module) would run pytest at
    commit OUTSIDE the fanout. Every pre-commit pytest child must be a
    _gate_argv() key carrying its own declared argv."""
    gates = _gate_argv()
    assert BATTERY_HOOK_ID in gates, f"fanout misses the battery child: {sorted(gates)}"
    assert gates[BATTERY_HOOK_ID][-3:] == ["-m", "pytest", "-q"]
    for hook_id, block in _hook_blocks(_config()):
        if "pre-commit" in str(_field(block, "stages")) and "-m pytest" in _entry(block):
            declared = shlex.split(_entry(block).split(" -- ", 1)[1])
            assert gates.get(hook_id) == declared, f"un-wired pytest child: {hook_id}"


def test_the_fanout_refuses_a_config_with_zero_gates(monkeypatch, tmp_path) -> None:
    """Pins the existing empty-config refusal: a config whose pre-commit hooks
    all lack the engine module must raise, never silently gate nothing."""
    empty = tmp_path / "pre-commit-config.yaml"
    empty.write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: ruff\n"
        "        entry: .venv/bin/python -m ruff check\n"
        "        stages: [pre-commit]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate_fanout, "CONFIG_PATH", empty)
    with pytest.raises(ValueError, match="declares no"):
        gate_fanout._gate_argv()
