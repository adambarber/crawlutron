"""Bite: multi-tag election.

Invokes scripts/release_roster.py and asserts every roster entry carries a
tag, so multi-tag election input is present.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_multi_tag_election_roster_tags():
    """release_roster.py emits entries each carrying a tag."""
    repo_root = Path(__file__).resolve().parents[2]
    venv_python = repo_root / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(venv_python), "scripts/release_roster.py"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"Roster resolution failed: {combined!r}"
    entries = json.loads(result.stdout)
    assert isinstance(entries, list) and entries, f"Expected non-empty roster, got: {combined!r}"
    for entry in entries:
        assert entry.get("tag"), f"Expected a tag on roster entry {entry!r}"
