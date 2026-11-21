"""Behavioral tests for scripts/coverage_reports.py."""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "coverage_reports.py"


def _load() -> ModuleType:
    """Load a fresh copy of the coverage report entrypoint."""
    spec = spec_from_file_location("_coverage_reports_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorder(commands: list[list[str]]) -> Callable[[list[str]], None]:
    def record(cmd: list[str]) -> None:
        commands.append(cmd)

    return record


def _noop(*args: object, **kwargs: object) -> None:
    return None


def test_missing_coverage_file_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absent .coverage refuses with a named error."""
    module = _load()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    assert module.main() == 1
    assert "Coverage data file not found" in capsys.readouterr().err


def test_repo_wide_reports_use_the_interpreter_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Coverage is invoked through the running interpreter, independent of PATH."""
    module = _load()
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_run", _recorder(commands))
    module._repo_wide_reports(tmp_path)
    assert len(commands) == 3
    for command in commands:
        assert command[:3] == [sys.executable, "-m", "coverage"]
    assert commands[0][3] == "xml"
    assert commands[1][3] == "html"
    assert "--format=markdown" in commands[2]


def test_per_package_reports_are_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each member gets its own directory and include filter."""
    module = _load()
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_run", _recorder(commands))
    module._per_package_reports(tmp_path, {"sdk": "packages/sdk/src"})
    assert (tmp_path / "sdk").is_dir()
    assert any("packages/sdk/src/*" in command for command in commands)


def test_include_patterns_derive_from_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Root and member patterns come from the manifest roster."""
    module = _load()
    monkeypatch.setattr(
        module,
        "roster",
        lambda: [
            {"name": "crawlutron", "path": "."},
            {"name": "sdk", "path": "packages/sdk"},
        ],
    )
    assert module._include_patterns() == {
        "crawlutron": "packages/*/src",
        "sdk": "packages/sdk/src",
    }


def test_reports_directory_is_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With data present, a successful run produces the reports directory."""
    module = _load()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    (tmp_path / ".coverage").touch()
    monkeypatch.setattr(module, "_include_patterns", lambda: {})
    monkeypatch.setattr(module, "_repo_wide_reports", _noop)
    monkeypatch.setattr(module, "_per_package_reports", _noop)
    assert module.main() == 0
    assert (tmp_path / "coverage_reports").is_dir()
