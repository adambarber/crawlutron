"""Bite: missing roster tag key.

Verifies specific ERROR message for missing required env var.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_missing_roster_tag_key_specific_message():
    """Verify specific ERROR message for missing required env var."""
    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    missing_var = "RASTER_TAG_KEY_THAT_DOES_NOT_EXIST"

    result = subprocess.run(
        [
            str(venv_python),
            "-c",
            f"import sys; sys.path.insert(0, 'scripts'); "
            f"from release._common import _require_env; "
            f"_require_env('{missing_var}')",
        ],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != missing_var},
    )

    combined = result.stdout + result.stderr
    assert f"ERROR: {missing_var} environment variable is required" in combined, (
        f"Expected specific error message in output ({combined!r}), got generic failure instead."
    )
