"""Emit the manifest-derived PyPI release roster as JSON.

Root project plus every ``[tool.uv.workspace].members`` package, each carrying
its declared ``project.name`` as both name and ``uv build --package`` selector.
"""

from __future__ import annotations

import json
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


def roster(root: Path = ROOT) -> list[dict[str, str]]:
    """Root project plus every workspace member, name from each manifest."""
    table = _manifest(root)
    root_project = table["project"]
    entries: list[dict[str, str]] = [
        {"name": str(root_project["name"]), "selector": str(root_project["name"])}
    ]
    for pattern in _members(table):
        for member in _expand(root, pattern):
            project = _project(member)
            entries.append({"name": str(project["name"]), "selector": str(project["name"])})
    return entries


def main() -> None:
    print(json.dumps(roster(), indent=2))


if __name__ == "__main__":
    main()
