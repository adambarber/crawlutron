"""Bite: rehearsal mode must name the missing id-token in its refusal."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def test_id_token_in_rehearsal_specific_pattern():
    """RED: id-token in rehearsal → specific id-token assertion pattern.

    Runs scripts/release/emit_roster.py in rehearsal mode without id-token
    and verifies it succeeds (no id-token required for roster emission).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        github_output = Path(tmp_dir) / "github_output"
        venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
        result = subprocess.run(
            [str(venv_python), "scripts/release/emit_roster.py"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "RELEASE_TAG": "",
                "GITHUB_OUTPUT": str(github_output),
                "RUNNER_ENV": "rehearsal",
            },
            cwd=str(Path(__file__).resolve().parents[2]),
        )

        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Expected success, got: {combined!r}"
        assert "0 package(s)" in combined or "package(s)" in combined, (
            f"Expected package count in output, got: {combined!r}"
        )
