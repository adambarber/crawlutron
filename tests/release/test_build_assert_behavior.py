"""Behavioral exit-code tests for build_assert."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.release._fake_run import FakeRun
from tests.release._script_loader import load_script

_MODULE = load_script("build_assert")


def _fake_wheel(root: Path, version: str) -> None:
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    with zipfile.ZipFile(dist / "package-1.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("package-1.0.dist-info/METADATA", f"Version: {version}\n")


def test_no_wheel_exits_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_MODULE, "_run", FakeRun())
    monkeypatch.setenv("SELECTOR", "package")
    monkeypatch.setenv("EXPECTED_VERSION", "1.0")

    assert _MODULE.main() == 3


def test_version_mismatch_exits_four(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_wheel(tmp_path, "9.9")
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_MODULE, "_run", FakeRun())
    monkeypatch.setenv("SELECTOR", "package")
    monkeypatch.setenv("EXPECTED_VERSION", "1.0")

    assert _MODULE.main() == 4


def test_matching_version_runs_twine_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_wheel(tmp_path, "1.0")
    run = FakeRun()
    monkeypatch.setattr(_MODULE, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_MODULE, "_run", run)
    monkeypatch.setenv("SELECTOR", "package")
    monkeypatch.setenv("EXPECTED_VERSION", "1.0")

    assert _MODULE.main() == 0
    assert any("twine" in command and "--strict" in command for command in run.commands)
