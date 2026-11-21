"""Bite: invalid workflow key.

Invokes scripts/pin_verify.py against a pin whose site SHA is not a commit SHA
and asserts the specific refusal message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORKFLOW = (
    "name: pv\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@not-a-sha  # v7.0.1\n"
)
WORKFLOW_NAME = "pvtest_invalid_key.yml"


def test_invalid_workflow_key_refusal_message(tmp_path):
    """pin_verify check refuses a pin that is not a commit SHA."""
    repo_root = Path(__file__).resolve().parents[2]
    workflow_path = repo_root / ".github" / "workflows" / WORKFLOW_NAME
    workflow_path.write_text(WORKFLOW)
    try:
        venv_python = repo_root / ".venv" / "bin" / "python"
        result = subprocess.run(
            [str(venv_python), "scripts/pin_verify.py", "check"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, f"Expected refusal, got: {combined!r}"
        assert "is not a commit SHA" in combined, f"Expected 'is not a commit SHA' refusal, got: {combined!r}"
    finally:
        workflow_path.unlink(missing_ok=True)
