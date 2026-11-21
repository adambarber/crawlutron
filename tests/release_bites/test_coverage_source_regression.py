"""Bite: coverage source regression.

`coverage_reports.py` refuses to build reports when the combined coverage data is
absent, with the exact diagnostic naming the file it looked for.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_missing_coverage_data_refusal_message(scripts_tree: Path):
    """The report step fails naming the missing `.coverage` file and its location."""
    result = subprocess.run(
        [sys.executable, "scripts/coverage_reports.py"],
        capture_output=True,
        text=True,
        cwd=str(scripts_tree),
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stderr.strip() == f"ERROR: Coverage data file not found at {scripts_tree / '.coverage'}"
