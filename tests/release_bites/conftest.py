"""Shared fixtures for the release-bite suite: disposable copies, never the live tree."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

REPO_ROOT = _SCRIPTS_DIR.parent


class PinTree:
    """A disposable copy of the pin-verify surface; the live tree is never written."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def add_workflow(self, name: str, body: str) -> None:
        """Add a workflow to the copy, as a commit-time author would."""
        (self.root / ".github" / "workflows" / name).write_text(body, encoding="utf-8")

    def pin(self, dependency: str) -> tuple[str, str]:
        """The (sha, version) pair the registry declares for a dependency."""
        registry = tomllib.loads((self.root / ".github" / "versions.toml").read_text(encoding="utf-8"))
        entry: dict[str, str] = registry["actions"][dependency]
        return entry["sha"], entry["version"]

    def check(self) -> subprocess.CompletedProcess[str]:
        """Run the copy's real `pin_verify.py check` from the copy root."""
        return subprocess.run(
            [sys.executable, "scripts/pin_verify.py", "check"],
            capture_output=True,
            text=True,
            cwd=str(self.root),
            check=False,
        )


@pytest.fixture
def pin_tree(tmp_path: Path) -> PinTree:
    """A disposable copy of every input `pin_verify.py check` reads."""
    root = tmp_path / "pin_tree"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "pin_verify.py", root / "scripts" / "pin_verify.py")
    (root / ".github").mkdir()
    shutil.copy2(REPO_ROOT / ".github" / "versions.toml", root / ".github" / "versions.toml")
    for surface in ("workflows", "actions"):
        source = REPO_ROOT / ".github" / surface
        if source.is_dir():
            shutil.copytree(source, root / ".github" / surface, ignore=shutil.ignore_patterns("__pycache__"))
    return PinTree(root)


@pytest.fixture
def scripts_tree(tmp_path: Path) -> Path:
    """A disposable copy of `scripts/`, so a bite never writes the live tree."""
    root = tmp_path / "scripts_tree"
    root.mkdir()
    shutil.copytree(_SCRIPTS_DIR, root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    return root
