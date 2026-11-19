"""Every package's version is a declared number, and the runtime reads that one."""

from __future__ import annotations

from pathlib import Path

import crawlutron
from tests._walk_primitives import workspace_package_dirs, workspace_project

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_every_package_declares_a_static_version() -> None:
    """release-please can only bump a number that exists in the manifest."""
    for path, directory in workspace_package_dirs(SERVICE_ROOT).items():
        project = workspace_project(directory)
        dynamic = project.get("dynamic", [])
        assert isinstance(dynamic, list)
        assert "version" not in dynamic, f"{path} still derives its version at build time"
        assert isinstance(project.get("version"), str), f"{path} declares no version"


def test_the_running_version_is_the_core_declaration() -> None:
    """One home for the number: the pyproject release-please rewrites."""
    assert crawlutron.__version__ == workspace_project(SERVICE_ROOT / "packages/core")["version"]
