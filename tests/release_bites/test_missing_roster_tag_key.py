"""Bite: missing roster tag key.

Verifies specific ERROR message for missing required env var.
"""

from __future__ import annotations

import os
import subprocess

from tests.chokepoint_paths import REPO_ROOT


def test_missing_roster_tag_key_specific_message():
    """Verify specific ERROR message for missing required env var."""
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    missing_var = "RASTER_TAG_KEY_THAT_DOES_NOT_EXIST"

    result = subprocess.run(
        [
            str(venv_python),
            "-c",
            f"from scripts.release._common import _require_env; _require_env('{missing_var}')",
        ],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != missing_var} | {"PYTHONPATH": str(REPO_ROOT)},
    )

    combined = result.stdout + result.stderr
    assert f"ERROR: {missing_var} environment variable is required" in combined, (
        f"Expected specific error message in output ({combined!r}), got generic failure instead."
    )
