"""Behavioral tests for the shared release helpers."""

from __future__ import annotations

import pytest

from tests.release._fake_run import FakeRun
from tests.release._script_loader import load_script


def test_gh_upload_passes_clobber_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    common = load_script("_common")
    run = FakeRun()
    monkeypatch.setattr(common, "_run", run)

    common._gh_release_upload("pkg-v1", "secret-token", ["a.whl"], "Upload failed")

    assert run.commands == [["gh", "release", "upload", "pkg-v1", "a.whl", "--clobber"]]
    assert run.environments[0] is not None
    assert run.environments[0]["GH_TOKEN"] == "secret-token"


def test_failed_gh_upload_exits_four(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    common = load_script("_common")
    monkeypatch.setattr(common, "_run", FakeRun(returncode=1, stderr="denied"))

    with pytest.raises(SystemExit) as refusal:
        common._gh_release_upload("pkg-v1", "token", [], "SBOM upload failed")

    assert refusal.value.code == 4
    assert "SBOM upload failed: denied" in capsys.readouterr().err
