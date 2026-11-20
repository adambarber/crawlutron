"""Emit the manifest-derived PyPI release roster as JSON.

Root project plus every ``[tool.uv.workspace].members`` package, each carrying
its declared name, version, and directory.
"""

from __future__ import annotations

import argparse
import json
import os
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
        "tag": f"{name}-v{project['version']}",
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


def released(root: Path = ROOT, tag: str | None = None) -> list[dict[str, str]]:
    """Roster entries whose ``<name>-v<version>`` tag is released."""
    tags = frozenset({tag}) if tag else _tags_at_head(root)
    return [entry for entry in roster(root) if f"{entry['name']}-v{entry['version']}" in tags]


def primary_tag(root: Path = ROOT, event_name: str = "", ref_name: str = "") -> bool:
    """Whether this event owns the publish side effects for the tags at HEAD."""
    if event_name == "workflow_dispatch":
        return True
    entries = roster(root)
    expected = {f"{entry['name']}-v{entry['version']}" for entry in entries}
    tags = _tags_at_head(root) & expected
    if not tags:
        return False
    root_tag = f"{entries[0]['name']}-v{entries[0]['version']}"
    chosen = root_tag if root_tag in tags else sorted(tags)[0]
    return ref_name == chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--released", action="store_true", help="only packages tagged at HEAD")
    parser.add_argument("--primary", action="store_true", help="is this event the publishing one")
    arguments = parser.parse_args()
    if arguments.primary:
        print(
            "true"
            if primary_tag(
                event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
                ref_name=os.environ.get("GITHUB_REF_NAME", ""),
            )
            else "false"
        )
        return
    entries = released(tag=os.environ.get("RELEASE_TAG") or None) if arguments.released else roster()
    print(json.dumps(entries, indent=2))


if __name__ == "__main__":
    main()
