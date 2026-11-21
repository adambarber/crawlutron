"""Behavioral tests for scripts/release/release_artifacts.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.release._script_loader import load_script
from tests.release._test_base import _make_script_runner

_run_script = _make_script_runner("release_artifacts.py")


def _completed(returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def test_missing_tag_is_error() -> None:
    """Missing TAG → exit 2."""
    result = _run_script({"GH_TOKEN": "dummy"})
    assert result.returncode == 2
    assert "TAG environment variable is required" in result.stderr


def test_missing_gh_token_is_error() -> None:
    """Missing GH_TOKEN → exit 2."""
    result = _run_script({"TAG": "crawlutron-v0.5.2"})
    assert result.returncode == 2
    assert "GH_TOKEN environment variable is required" in result.stderr


def test_missing_dist_dir_is_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No dist/ directory refuses collection regardless of the host tree."""
    module = load_script("release_artifacts")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit) as exc:
        module._collect_files()
    assert exc.value.code == 3
    assert "no dist/ directory found" in capsys.readouterr().err


def test_checksum_generation_targets_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checksums are built inside dist/ through one shell invocation."""
    module = load_script("release_artifacts")
    commands: list[list[str]] = []

    def record(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return _completed(0)

    monkeypatch.setattr(module, "_run", record)
    module._build_checksums()
    assert commands[0][:2] == ["sh", "-c"]
    assert "dist" in commands[0][2]
    assert "sha256sum" in commands[0][2]


def test_checksum_failure_is_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing checksum command refuses with exit 3."""
    module = load_script("release_artifacts")
    monkeypatch.setattr(module, "_run", lambda cmd, **kwargs: _completed(1))
    with pytest.raises(SystemExit) as exc:
        module._build_checksums()
    assert exc.value.code == 3
    assert "Checksum generation failed" in capsys.readouterr().err


def test_collects_wheel_sdist_and_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Upload set is exactly the wheel, sdist, and SHA256SUMS."""
    module = load_script("release_artifacts")
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("pkg.whl", "pkg.tar.gz", "SHA256SUMS"):
        (dist / name).touch()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    files = module._collect_files()
    assert {Path(name).name for name in files} == {"pkg.whl", "pkg.tar.gz", "SHA256SUMS"}


def test_no_wheel_fails_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty dist/ cannot produce an empty release."""
    module = load_script("release_artifacts")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "SHA256SUMS").touch()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit) as exc:
        module._collect_files()
    assert exc.value.code == 3
    assert "no wheel or sdist found" in capsys.readouterr().err


def test_main_uploads_the_collected_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() passes the collected artifacts to the shared upload chokepoint."""
    module = load_script("release_artifacts")
    monkeypatch.setenv("TAG", "crawlutron-v1.0.0")
    monkeypatch.setenv("GH_TOKEN", "dummy")
    monkeypatch.setattr(module, "_build_checksums", lambda: None)
    monkeypatch.setattr(module, "_collect_files", lambda: ["dist/pkg.whl", "dist/SHA256SUMS"])
    uploads: list[list[str]] = []

    def capture_upload(tag: str, token: str, files: list[str], error: str) -> None:
        uploads.append(files)

    monkeypatch.setattr(module, "_gh_release_upload", capture_upload)
    assert module.main() == 0
    assert uploads == [["dist/pkg.whl", "dist/SHA256SUMS"]]
