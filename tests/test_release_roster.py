"""The release roster derives from the manifest, never a hand-typed list."""

from __future__ import annotations

import tomllib

import yaml

from scripts.release_roster import roster
from tests._ci_contract import SERVICE_ROOT

RELEASE_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "release.yml"
ROOT_MANIFEST = SERVICE_ROOT / "pyproject.toml"


def _manifest_roster() -> list[dict[str, str]]:
    """The roster the manifest dictates: root project plus every member by project.name."""
    table = tomllib.loads(ROOT_MANIFEST.read_text(encoding="utf-8"))
    entries: list[dict[str, str]] = [
        {"name": str(table["project"]["name"]), "selector": str(table["project"]["name"])}
    ]
    workspace = table.get("tool", {}).get("uv", {}).get("workspace", {})
    for pattern in workspace.get("members", ()):
        for member in sorted(SERVICE_ROOT.glob(pattern)):
            if member.is_dir():
                project = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))["project"]
                entries.append({"name": str(project["name"]), "selector": str(project["name"])})
    return entries


def test_roster_matches_the_manifest() -> None:
    assert roster() == _manifest_roster()


def test_release_matrix_comes_from_the_roster_script() -> None:
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    distributions = jobs.get("distributions")
    assert isinstance(distributions, dict)
    strategy = distributions.get("strategy")
    assert isinstance(strategy, dict)
    matrix = strategy.get("matrix")
    assert isinstance(matrix, str), "the publish matrix must be a dynamic expression, not an inline list"
    assert matrix.startswith("${{") and matrix.endswith("}}")
    assert "fromJson(needs.roster.outputs.matrix)" in matrix
    for entry in _manifest_roster():
        assert f'"{entry["name"]}"' not in matrix, f"{entry['name']!r} is hand-typed into the publish matrix"
