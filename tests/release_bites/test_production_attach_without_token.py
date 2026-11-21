"""Bite: production-mode attach without token.

Verifies specific token-absence message for missing GH_TOKEN.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_production_attach_without_token_specific_message(tmp_path):
    """Verify specific token-absence message for missing GH_TOKEN."""
    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    result = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import sys\n"
            "sys.path.insert(0, 'scripts')\n"
            "from release._common import _release_credentials\n"
            "_release_credentials()",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "TAG": "test-v1.0.0"},
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, "Expected _release_credentials to fail without GH_TOKEN"
    assert "GH_TOKEN" in combined or "token" in combined.lower(), (
        f"Expected specific error message about missing GH_TOKEN, got: {combined!r}"
    )
