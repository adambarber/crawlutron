"""Emit the released packages roster as compact GITHUB_OUTPUT and a one-line summary.

This replaces the inline python in release.yml's `released` job plus the
separate `python3 -` block that sets GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The repo root is two levels up from this script (scripts/release/emit_roster.py)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _manifest(root: Path = REPO_ROOT) -> dict:
    import tomllib
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
    return tuple(
        sorted(path for path in root.glob(pattern) if path.is_dir())
    )


def _project(path: Path):
    import tomllib
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


def roster(root: Path = REPO_ROOT) -> list[dict[str, str]]:
    """Root project plus every workspace member, name from each manifest."""
    table = _manifest(root)
    entries = [_entry(root, root)]
    for pattern in _members(table):
        entries.extend(_entry(root, member) for member in _expand(root, pattern))
    return entries


def _tags_at_head(root: Path) -> frozenset[str]:
    import shutil
    git = shutil.which("git") or "git"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    done = subprocess.run(
        [git, "tag", "--points-at", "HEAD"],
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return frozenset(done.stdout.split())


def released(root: Path = REPO_ROOT, tag: str | None = None) -> list[dict[str, str]]:
    """Roster entries whose ``<name>-v<version>`` tag is released."""
    tags = frozenset({tag}) if tag else _tags_at_head(root)
    entries = roster(root)
    return [entry for entry in entries if f"{entry['name']}-v{entry['version']}" in tags]


def primary_tag(root: Path = REPO_ROOT, event_name: str = "", ref_name: str = "") -> bool:
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
        result = primary_tag()
        print("true" if result else "false")
        return

    # Resolve RELEASE_TAG: empty string means "tags at HEAD"
    release_tag = os.environ.get("RELEASE_TAG")
    entries = released(root=REPO_ROOT, tag=release_tag) if release_tag else roster()

    # Determine root_entry and primary_result early for stdout and GITHUB_OUTPUT
    root_entry = next((e for e in entries if e["name"] == "crawlutron"), None)
    primary_result = primary_tag()

    # Write GITHUB_OUTPUT
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as out:
            # packages=<JSON array> — compact, matching release.yml:92-94
            out.write(f"packages={json.dumps(entries)}\n")
            # root_version=<string> — version of the crawlutron entry, or ""
            root_version = root_entry["version"] if root_entry else ""
            out.write(f"root_version={root_version}\n")
            # primary=<true|false> — result of release_roster.py --primary
            out.write(f"primary={('true' if primary_result else 'false')}\n")

    # Stdout: one-line summary
    root_version = root_entry["version"] if root_entry else ""
    primary_str = "primary=true" if primary_result else "primary=false"
    print(f"{len(entries)} package(s) tagged; root={root_version or 'not released'}; {primary_str}")


if __name__ == "__main__":
    main()