"""The roster derives from the manifests, and "released" from the tags at HEAD."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import yaml

from scripts.release_roster import released, roster

RELEASE_WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
GIT = shutil.which("git") or "git"


def _git(root: Path, *arguments: str) -> None:
    """Run git pinned to ``root`` by environment, so a hook's git vars cannot redirect it.

    A test identity is set so ``git commit`` works in any environment, including CI,
    where no ``user.name``/``user.email`` is configured.
    """
    environment = {
        **os.environ,
        "GIT_DIR": str(root / ".git"),
        "GIT_WORK_TREE": str(root),
        "GIT_INDEX_FILE": str(root / ".git/index"),
        "GIT_AUTHOR_NAME": "Crawlutron Test Suite",
        "GIT_AUTHOR_EMAIL": "test@crawlutron.local",
        "GIT_COMMITTER_NAME": "Crawlutron Test Suite",
        "GIT_COMMITTER_EMAIL": "test@crawlutron.local",
    }
    subprocess.run([GIT, *arguments], cwd=root, env=environment, check=True, capture_output=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stand_up(tmp_path: Path, member_version: str = "2.0.0") -> Path:
    _write(
        tmp_path / "pyproject.toml",
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n\n[project]\nname = "app"\nversion = "1.0.0"\n',
    )
    _write(
        tmp_path / "packages/one/pyproject.toml",
        f'[project]\nname = "one"\nversion = "{member_version}"\n',
    )
    for command in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "init")):
        _git(tmp_path, *command)
    return tmp_path


def _tag(root: Path, name: str) -> None:
    _git(root, "tag", name)


def test_the_roster_derives_name_version_and_path_from_each_manifest(tmp_path: Path) -> None:
    root = _stand_up(tmp_path)
    entries = {entry["name"]: entry for entry in roster(root)}
    assert set(entries) == {"app", "one"}
    assert entries["one"]["version"] == "2.0.0"
    assert entries["one"]["path"] == "packages/one"
    assert entries["app"]["path"] == "."


def test_only_packages_whose_version_tag_points_at_head_are_released(tmp_path: Path) -> None:
    """A release-please tag names the exact version, so a stale tag releases nothing."""
    root = _stand_up(tmp_path)
    _tag(root, "one-v2.0.0")
    _tag(root, "app-v9.9.9")
    assert [entry["name"] for entry in released(root)] == ["one"]


def test_a_bumped_version_without_its_tag_releases_nothing(tmp_path: Path) -> None:
    """The tag and the declared version must agree; a bump alone is not a release."""
    root = _stand_up(tmp_path)
    _tag(root, "one-v2.0.0")
    text = (root / "packages/one/pyproject.toml").read_text(encoding="utf-8")
    _write(root / "packages/one/pyproject.toml", text.replace("2.0.0", "2.1.0"))
    assert tomllib.loads(text)["project"]["version"] == "2.0.0"
    assert released(root) == []


def test_the_publish_matrix_is_the_released_set_never_a_hand_typed_list() -> None:
    """A literal name in the matrix is a package the roster does not govern."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    matrix = document["jobs"]["publish"]["strategy"]["matrix"]
    rendered = " ".join(str(value) for value in matrix.values())
    assert "fromJSON(needs.released.outputs.packages)" in rendered
    assert "fromJSON(needs.roster" not in rendered, "the roster job is the writer; never re-parse it"
