"""Bite: production-mode attach without token.

Invokes scripts/release/release_artifacts.py without GH_TOKEN and asserts
the specific credential-refusal message.
"""

from __future__ import annotations

import os
import subprocess

from tests.chokepoint_paths import REPO_ROOT


def test_production_attach_without_token_refusal_message():
    """release_artifacts.py refuses when GH_TOKEN is missing."""
    repo_root = REPO_ROOT
    venv_python = repo_root / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(venv_python), "-m", "scripts.release.release_artifacts"],
        capture_output=True,
        text=True,
        env={**os.environ, "TAG": "test-v1.0.0"},
        cwd=str(repo_root),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"Expected refusal, got: {combined!r}"
    assert "ERROR: GH_TOKEN environment variable is required" in combined, (
        f"Expected specific refusal, got: {combined!r}"
    )
