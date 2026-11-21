"""Bite: partial Dockerfile copy.

Invokes scripts/release/build_assert.py without the required SELECTOR env
and asserts the specific refusal message.
"""

from __future__ import annotations

import os
import subprocess

from tests.chokepoint_paths import REPO_ROOT


def test_partial_dockerfile_copy_refusal_message():
    """build_assert.py refuses when SELECTOR is missing."""
    repo_root = REPO_ROOT
    venv_python = repo_root / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(venv_python), "-m", "scripts.release.build_assert"],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "SELECTOR"},
        cwd=str(repo_root),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"Expected refusal, got: {combined!r}"
    assert "ERROR: SELECTOR environment variable is required" in combined, (
        f"Expected specific refusal, got: {combined!r}"
    )
