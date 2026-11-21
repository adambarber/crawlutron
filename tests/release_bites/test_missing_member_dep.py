"""Bite: missing member dep.

Invokes scripts/pin_verify.py against a deliberately unregistered dependency
pin and asserts the specific refusal message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORKFLOW = (
    "name: pv\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: owner/unregistered-dep@a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n"
)
WORKFLOW_NAME = "pvtest_member_dep.yml"


def test_missing_member_dep_refusal_message(tmp_path):
    """pin_verify check refuses an unregistered dependency pin."""
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
        assert "unregistered dependency" in combined, (
            f"Expected 'unregistered dependency' refusal, got: {combined!r}"
        )
    finally:
        workflow_path.unlink(missing_ok=True)
