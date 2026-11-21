"""Bite: image version 0.0.0.

Invokes scripts/pin_verify.py against a pin declaring a wrong version and
asserts the specific version-mismatch refusal message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORKFLOW = (
    "name: pv\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b0  # v99.99.99\n"
)
WORKFLOW_NAME = "pvtest_version.yml"


def test_image_version_0_0_0_refusal_message(tmp_path):
    """pin_verify check refuses a version mismatch against the registry."""
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
        assert "registry says" in combined, (
            f"Expected 'registry says' version-mismatch refusal, got: {combined!r}"
        )
    finally:
        workflow_path.unlink(missing_ok=True)
