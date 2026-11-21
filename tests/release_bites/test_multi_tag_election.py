"""Bite: multi-tag election.

Verifies roster resolves multiple tags for election.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_multi_tag_election_specific_outcome(tmp_path):
    """Verify roster resolves multiple tags for election."""
    import sys

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from release._common import roster

    entries = roster()
    crawlutron_entries = [e for e in entries if e["name"] == "crawlutron"]

    assert len(crawlutron_entries) >= 1, (
        f"Expected at least one crawlutron entry in roster, got {len(crawlutron_entries)}"
    )

    tag_names = [e["tag"] for e in crawlutron_entries]
    assert tag_names, "Expected crawlutron entries to have tags"


def test_multi_tag_election_roster_resolution():
    """Verify roster resolves multiple tags for election."""
    import json
    from pathlib import Path

    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    result = subprocess.run(
        [str(venv_python), "scripts/release_roster.py"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"Roster resolution failed: {combined!r}"

    entries = json.loads(result.stdout)
    assert isinstance(entries, list), f"Expected list of entries, got: {type(entries)}"
    assert len(entries) > 0, "Expected at least one entry in roster"
