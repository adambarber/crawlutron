"""Bite: required-aggregate middle-failure.

Invokes scripts/release/install_graph.py with NAME set but VERSION missing
and asserts the specific refusal message.
"""

from __future__ import annotations

import os
import subprocess

from tests.chokepoint_paths import REPO_ROOT


def test_required_aggregate_middle_failure_refusal_message():
    """install_graph.py refuses when VERSION is missing."""
    repo_root = REPO_ROOT
    venv_python = repo_root / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(venv_python), "-m", "scripts.release.install_graph"],
        capture_output=True,
        text=True,
        env={**os.environ, "NAME": "crawlutron"},
        cwd=str(repo_root),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"Expected refusal, got: {combined!r}"
    assert "ERROR: VERSION environment variable is required" in combined, (
        f"Expected specific refusal, got: {combined!r}"
    )
