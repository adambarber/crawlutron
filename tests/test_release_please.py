"""release-please owns every version: the config, the manifest, and its workflow."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests._walk_primitives import workspace_package_dirs, workspace_project

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / "release-please-config.json"
MANIFEST_PATH = SERVICE_ROOT / ".release-please-manifest.json"
WORKFLOW_PATH = SERVICE_ROOT / ".github/workflows/release-please.yml"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _config() -> dict[str, object]:
    return _mapping(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))


def _steps(job: object) -> list[dict[str, object]]:
    """One job's step records, each narrowed to a string-keyed mapping."""
    steps = _mapping(job).get("steps")
    assert isinstance(steps, list)
    return [_mapping(step) for step in steps]


def test_every_workspace_package_is_configured_for_release_please() -> None:
    """A member outside the config is a package no release ever bumps."""
    assert CONFIG_PATH.exists(), "release-please-config.json is missing"
    packages = _mapping(_config()["packages"])
    assert set(packages) == set(workspace_package_dirs(SERVICE_ROOT))


def test_each_package_names_its_own_distribution_and_tag() -> None:
    """release-please's python type cannot look the name up; it must be declared."""
    packages = _mapping(_config()["packages"])
    for path, directory in workspace_package_dirs(SERVICE_ROOT).items():
        entry = _mapping(packages[path])
        name = workspace_project(directory)["name"]
        assert entry["package-name"] == name, f"{path} names the wrong distribution"
        assert entry["component"] == name, f"{path} would tag under another component"


def test_the_manifest_equals_every_declared_version() -> None:
    """Two copies of a version number drift; this gate holds them equal."""
    assert MANIFEST_PATH.exists(), ".release-please-manifest.json is missing"
    manifest = _mapping(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
    assert set(manifest) == set(workspace_package_dirs(SERVICE_ROOT))
    for path, directory in workspace_package_dirs(SERVICE_ROOT).items():
        assert manifest[path] == workspace_project(directory)["version"], f"{path} manifest version disagrees"


def test_packages_version_independently() -> None:
    """Independent versioning: no plugin may link the group into one number."""
    config = _config()
    plugins = config.get("plugins", [])
    assert isinstance(plugins, list)
    names = {plugin if isinstance(plugin, str) else _mapping(plugin)["type"] for plugin in plugins}
    assert "linked-versions" not in names
    assert config.get("separate-pull-requests") is not True, "one release PR keeps the lock refresh single"


def test_the_release_pr_refreshes_the_lock_for_the_bumped_versions() -> None:
    """A version bump makes uv.lock stale; the release PR must carry the re-lock."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = _mapping(document["jobs"])
    runs = "\n".join(str(step.get("run", "")) for job in jobs.values() for step in _steps(job))
    assert "uv lock" in runs, "the release PR must re-lock for the versions it bumps"
    assert "git push" in runs, "the refreshed lock must reach the release branch"


def test_the_published_version_is_the_manifest_version() -> None:
    """A version parsed out of a branch name would name artifacts after `main`."""
    text = (SERVICE_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "GITHUB_REF_NAME" not in text, "a workflow_run's ref name is the default branch"
    assert "EXPECTED_VERSION: ${{ matrix.package.version }}" in text, (
        "the published version must come from the manifest-resolved roster"
    )


def test_release_please_runs_only_on_main_and_is_pinned() -> None:
    """Releases are driven by merges to main, from a commit-pinned action."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    trigger = document[True] if True in document else document["on"]
    assert trigger["push"]["branches"] == ["main"]
    jobs = _mapping(document["jobs"])
    action = [str(step["uses"]) for job in jobs.values() for step in _steps(job) if "uses" in step]
    assert any(uses.startswith("googleapis/release-please-action@") for uses in action)
    for uses in action:
        reference = uses.rsplit("@", 1)[-1]
        assert len(reference) == 40 and all(c in "0123456789abcdef" for c in reference), (
            f"{uses} must pin a full commit SHA"
        )
