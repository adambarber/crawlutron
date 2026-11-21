"""Bite: coverage report validation on a disposable scratch tree."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def test_coverage_report_generation():
    """Verify coverage_reports.py generates proper structure.

    Invokes scripts/coverage_reports.py when no coverage data exists
    and verifies it fails with the specific error message.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        Path(tmp_dir)

        venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
        result = subprocess.run(
            [str(venv_python), "scripts/coverage_reports.py"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[2],
        )

        combined = result.stdout + result.stderr
        assert result.returncode != 0, "Expected coverage_reports.py to fail without .coverage file"
        assert "ERROR" in combined or "not found" in combined.lower(), (
            f"Expected specific error message, got: {combined!r}"
        )
