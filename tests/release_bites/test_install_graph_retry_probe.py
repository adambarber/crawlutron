"""Bite: install-graph retry probe.

Invokes scripts/release/install_graph.py without NAME and asserts the
specific refusal message.
"""

from __future__ import annotations

import os
import subprocess

from tests.chokepoint_paths import REPO_ROOT


def test_install_graph_retry_probe_refusal_message():
    """install_graph.py refuses when NAME is missing."""
    repo_root = REPO_ROOT
    venv_python = repo_root / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(venv_python), "-m", "scripts.release.install_graph"],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in ("NAME", "VERSION")},
        cwd=str(repo_root),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"Expected refusal, got: {combined!r}"
    assert "ERROR: NAME environment variable is required" in combined, (
        f"Expected specific refusal, got: {combined!r}"
    )
