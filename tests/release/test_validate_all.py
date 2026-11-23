"""Preflight gate: one failed or missing package blocks every upload."""

from __future__ import annotations

import json
from pathlib import Path

from tests.release._fake_run import FakeRun
from tests.release._script_loader import load_script

_MODULE = load_script("validate_all")


def _stage(root: Path, selector: str, version: str) -> None:
    staged = root / "dist" / f"dist-{selector}"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / f"{selector}-{version}-py3-none-any.whl").write_bytes(b"wheel")


def _roster(monkeypatch, entries: list[dict[str, str]]) -> None:
    monkeypatch.setenv("PACKAGES", json.dumps(entries))


def test_one_failed_package_blocks_every_upload() -> None:
    """A single validation failure refuses the whole immutable version set."""
    roster = ("alpha", "beta", "gamma")
    validation = {"alpha": True, "beta": False, "gamma": True}
    assert _MODULE.may_publish(validation, roster) is False


def test_a_missing_package_blocks_every_upload() -> None:
    """An unvalidated roster selector is as fatal as a failed one."""
    assert _MODULE.may_publish({"alpha": True}, ["alpha", "beta"]) is False


def test_an_unexpected_package_blocks_every_upload() -> None:
    """A validation outside the roster cannot pad completeness."""
    assert _MODULE.may_publish({"alpha": True, "beta": True}, ["alpha"]) is False


def test_a_clean_complete_roster_allows_upload() -> None:
    """Every selector present and passing is the only green shape."""
    assert _MODULE.may_publish({"alpha": True, "beta": True}, ["alpha", "beta"]) is True


def test_gate_refuses_when_one_package_is_absent(tmp_path: Path, monkeypatch) -> None:
    """The real gate exits nonzero when a staged dist is missing."""
    entries = [{"selector": "alpha", "version": "1.0"}, {"selector": "beta", "version": "2.0"}]
    _stage(tmp_path, "alpha", "1.0")
    _roster(monkeypatch, entries)
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_MODULE, "_run", FakeRun())

    assert _MODULE.main() != 0


def test_gate_checks_every_dist_when_roster_is_complete(tmp_path: Path, monkeypatch) -> None:
    """A complete roster runs strict twine over every staged distribution."""
    entries = [{"selector": "alpha", "version": "1.0"}, {"selector": "beta", "version": "2.0"}]
    _stage(tmp_path, "alpha", "1.0")
    _stage(tmp_path, "beta", "2.0")
    _roster(monkeypatch, entries)
    run = FakeRun()
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_MODULE, "_run", run)

    assert _MODULE.main() == 0
    assert any("twine" in command and "--strict" in command for command in run.commands)
