"""No wheel build may declare a readme/license-files path whose file is absent.

Hatchling fails the build when a declared path field is missing, and no gate
exercises wheel builds -- parse each member pyproject and check the paths.
"""

from __future__ import annotations

import pathlib
import tomllib

from tests.chokepoint_walks import REPO_ROOT

_PACKAGE_DIR = REPO_ROOT / "packages"
#: Fields whose value is a repo-relative path (or list of paths) hatch requires to exist.
_PATH_FIELDS = ("readme", "license-files")


def _member_pyprojects() -> tuple[tuple[str, pathlib.Path, dict[str, object]], ...]:
    return tuple(
        (path.parent.name, path, tomllib.loads(path.read_text(encoding="utf-8")))
        for path in sorted(_PACKAGE_DIR.glob("*/pyproject.toml"))
    )


def _missing_path_fields(pyproject: dict[str, object], package_dir: pathlib.Path) -> list[str]:
    missing: list[str] = []
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return missing
    for field in _PATH_FIELDS:
        value = project.get(field)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        for name in value:
            if not isinstance(name, str) or not (package_dir / name).exists():
                missing.append(field)
    return missing


def test_every_member_pyproject_path_field_resolves() -> None:
    offenders: list[str] = []
    for name, path, pyproject in _member_pyprojects():
        for field in _missing_path_fields(pyproject, path.parent):
            offenders.append(f"{name}: {field} names a file not present under packages/{name}/")
    assert not offenders, "\n".join(offenders)
