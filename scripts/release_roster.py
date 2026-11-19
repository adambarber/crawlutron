"""Emit the manifest-derived PyPI release roster as JSON.

Root project plus every ``[tool.uv.workspace].members`` package, each carrying
its declared name, version, and directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _manifest(root: Path = ROOT):
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def _members(table) -> tuple[str, ...]:
    """The member patterns declared by the root workspace manifest."""
    workspace = table.get("tool", {}).get("uv", {}).get("workspace", {})
    return tuple(str(pattern) for pattern in workspace.get("members", ()))


def _expand(root: Path, pattern: str) -> tuple[Path, ...]:
    """The directories a member pattern names; a glob expands to its matches."""
    candidate = root / pattern
    if candidate.is_dir():
        return (candidate,)
    return tuple(sorted(path for path in root.glob(pattern) if path.is_dir()))


def _project(path: Path):
    return tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _entry(root: Path, directory: Path) -> dict[str, str]:
    project = _project(directory)
    name = str(project["name"])
    return {
        "name": name,
        "selector": name,
        "version": str(project["version"]),
        "path": "." if directory == root else directory.relative_to(root).as_posix(),
    }


def roster(root: Path = ROOT) -> list[dict[str, str]]:
    """Root project plus every workspace member, name from each manifest."""
    table = _manifest(root)
    entries = [_entry(root, root)]
    for pattern in _members(table):
        entries.extend(_entry(root, member) for member in _expand(root, pattern))
    return entries


def _tags_at_head(root: Path) -> frozenset[str]:
    git = shutil.which("git") or "git"
    done = subprocess.run(
        [git, "tag", "--points-at", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return frozenset(done.stdout.split())


def released(root: Path = ROOT) -> list[dict[str, str]]:
    """Roster entries whose ``<name>-v<version>`` tag points at HEAD."""
    tags = _tags_at_head(root)
    return [entry for entry in roster(root) if f"{entry['name']}-v{entry['version']}" in tags]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--released", action="store_true", help="only packages tagged at HEAD")
    arguments = parser.parse_args()
    entries = released() if arguments.released else roster()
    print(json.dumps(entries, indent=2))


if __name__ == "__main__":
    main()
