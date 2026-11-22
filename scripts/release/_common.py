"""Common utilities for release scripts."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from utils.paths import REPO_ROOT

UV = "uv"


def _run(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command in the repo root."""
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=capture,
        text=True,
        check=check,
        env=env,
    )


def _require_env(var: str) -> str:
    """Get required environment variable or exit with error."""
    value = os.environ.get(var, "")
    if not value:
        print(f"ERROR: {var} environment variable is required", file=sys.stderr)
        sys.exit(2)
    return value


def _require_env_optional(var: str) -> str:
    """Get optional environment variable."""
    return os.environ.get(var, "")


def _release_credentials() -> tuple[str, str]:
    """The tag and token every release-attachment step needs."""
    return _require_env("TAG"), _require_env("GH_TOKEN")


def _gh_release_upload(tag: str, token: str, files: list[str], error: str) -> None:
    """Attach files to a release, refusing with a named error on failure."""
    result = _run(
        ["gh", "release", "upload", tag, *files, "--clobber"],
        capture=True,
        env={**os.environ, "GH_TOKEN": token},
    )
    if result.returncode != 0:
        print(f"{error}: {result.stderr}", file=sys.stderr)
        sys.exit(4)


def _manifest(root: Path = REPO_ROOT) -> dict[str, object]:
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


def _project(path: Path) -> dict[str, object]:
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


def _tags_at_head(root: Path = REPO_ROOT) -> frozenset[str]:
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
