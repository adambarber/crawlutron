"""Bite: a release tag must name the commit that is actually checked out."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.release._common import _run
from tests.chokepoint_paths import REPO_ROOT
from tests.release._test_base import _run_script

EXPECTED_REFUSAL = "release tag does not point at HEAD: refusing to emit roster"
OLD_TAG = "crawlutron-v0.3.0"
MANIFEST = '[project]\nname = "crawlutron"\nversion = "0.3.0"\n'


def _git(root: Path, *argv: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    command = ["git", "-C", str(root), "-c", "user.name=bite", "-c", "user.email=bite@example.invalid", *argv]
    result = _run(command, env=environment)
    assert result.returncode == 0, result.stderr


def test_tag_pointing_at_another_commit_refuses_the_roster(scripts_tree: Path, tmp_path: Path) -> None:
    """A tag on an older commit than HEAD refuses instead of emitting a roster."""
    root = scripts_tree
    (root / "pyproject.toml").write_text(MANIFEST, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tagged release")
    _git(root, "tag", OLD_TAG)
    (root / "later").write_text("unrelated tree\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "later commit")

    output = tmp_path / "github_output"
    result = _run_script(
        REPO_ROOT / "scripts" / "release" / "emit_roster.py",
        {"RELEASE_TAG": OLD_TAG, "GITHUB_OUTPUT": str(output)},
        cwd=root,
        unset=tuple(key for key in os.environ if key.startswith("GIT_")),
    )

    assert result.returncode == 4, result.stdout + result.stderr
    assert EXPECTED_REFUSAL in result.stderr
    assert not output.exists()
