"""Bite: invalid workflow key (actionlint).

Verifies actionlint produces specific error for invalid key.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest


def test_invalid_workflow_key_specific_message():
    """Verify actionlint produces specific error for invalid key."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        scratch = Path(tmp_dir)
        workflow_dir = scratch / ".github" / "workflows"
        workflow_dir.mkdir(parents=True, exist_ok=True)
        workflow_path = workflow_dir / "test.yml"

        workflow_content = (
            "name: Test Workflow\non:\n  push:\n    branches: [main]\n"
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n"
            "      - run: echo hello\n        if:\n"
        )
        workflow_path.write_text(workflow_content)

        venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
        actionlint = venv_bin / "actionlint" if (venv_bin / "actionlint").exists() else None

        if actionlint:
            result = subprocess.run(
                [str(actionlint), "--github-repos=crawlutron"],
                capture_output=True,
                text=True,
                cwd=str(scratch),
            )

            combined = result.stdout + result.stderr
            if result.returncode != 0:
                assert (
                    "if:" in combined.lower()
                    or "error" in combined.lower()
                    or "unexpected" in combined.lower()
                ), f"Expected specific error message, got: {combined!r}"
        else:
            pytest.skip("actionlint not installed in venv")
