"""hatch-vcs owns version derivation; _version.py stays honest vs git describe."""

from __future__ import annotations

import shutil
import subprocess

from crawlutron import __version__
from crawlutron._version import __version__ as artifact_version
from crawlutron._version import __version_tuple__ as artifact_tuple


def _describe() -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git not found on PATH")
    return subprocess.run(
        [git, "describe", "--tags", "--long", "--always"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _version_parts(desc: str) -> tuple[str, tuple[int | str, ...]]:
    import re

    match = re.match(r"^v?(.*?)-(\d+)-g([0-9a-f]+)$", desc)
    if match:
        tag = f"{match.group(1)}.dev{match.group(2)}+g{match.group(3)}"
    else:
        tag = desc
    return tag, tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-]", tag))


def test_artifact_version_matches_git_describe() -> None:
    expected, expected_tuple = _version_parts(_describe())
    assert artifact_version == expected
    assert artifact_tuple == expected_tuple


def test_public_version_matches_the_artifact() -> None:
    assert __version__ == artifact_version
