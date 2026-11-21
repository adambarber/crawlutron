"""Bite: multi-tag election.

Two tags point at one commit; the elected publishing tag is the root project's,
so the sibling tag's event does not own publish side effects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from release._common import primary_tag

ROOT_MANIFEST = (
    '[project]\nname = "crawlutron"\nversion = "1.2.3"\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n'
)
MEMBER_MANIFEST = '[project]\nname = "alpha"\nversion = "1.2.3"\n'
ROOT_TAG = "crawlutron-v1.2.3"
MEMBER_TAG = "alpha-v1.2.3"


def _git(root: Path, *argv: str) -> None:
    git = shutil.which("git")
    assert git is not None, "git must be on PATH to build the election fixture"
    # GIT_* from an enclosing hook redirect git at the workspace; strip it.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(
        [git, "-c", "user.name=bite", "-c", "user.email=bite@example.invalid", *argv],
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def _tagged_at_one_commit(tmp_path: Path) -> Path:
    """A disposable repo where the root and a member tag point at one commit."""
    root = tmp_path / "election"
    (root / "packages" / "alpha").mkdir(parents=True)
    (root / "pyproject.toml").write_text(ROOT_MANIFEST, encoding="utf-8")
    (root / "packages" / "alpha" / "pyproject.toml").write_text(MEMBER_MANIFEST, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release")
    _git(root, "tag", MEMBER_TAG)
    _git(root, "tag", ROOT_TAG)
    return root


def test_multi_tag_election_prefers_the_root_project_tag(tmp_path: Path):
    """The root tag is elected though the member tag sorts first; only it publishes."""
    root = _tagged_at_one_commit(tmp_path)
    assert primary_tag(root=root, event_name="push", ref_name=ROOT_TAG) is True
    assert primary_tag(root=root, event_name="push", ref_name=MEMBER_TAG) is False
