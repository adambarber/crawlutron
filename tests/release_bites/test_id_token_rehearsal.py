"""Bite: id-token rehearsal — exact summary and GITHUB_OUTPUT payloads."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.release_bites.conftest import REPO_ROOT

ABSENT_TAG = "v0.0.0-no-package-carries-this"


def test_emit_roster_writes_the_release_outputs(tmp_path: Path):
    """A rehearsal run needs no id-token and writes its exact summary and outputs."""
    github_output = tmp_path / "github_output"
    result = subprocess.run(
        [sys.executable, "scripts/release/emit_roster.py"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "RELEASE_TAG": ABSENT_TAG,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_OUTPUT": str(github_output),
        },
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0 package(s) tagged; root=not released; primary=true\n"
    assert github_output.read_text(encoding="utf-8") == "packages=[]\nroot_version=\nprimary=true\n"
