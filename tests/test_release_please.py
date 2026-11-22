"""release-please owns every version: the config, the manifest, and its workflow."""

from __future__ import annotations

import json

import yaml

from tests._ci_contract import workflow_files
from tests._walk_primitives import workspace_package_dirs, workspace_project
from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
CONFIG_PATH = SERVICE_ROOT / "release-please-config.json"
MANIFEST_PATH = SERVICE_ROOT / ".release-please-manifest.json"
WORKFLOW_PATH = SERVICE_ROOT / ".github/workflows/release-please.yml"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _config() -> dict[str, object]:
    return _mapping(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))


def _steps(job: object) -> list[dict[str, object]]:
    """One job's step records; `uses:` jobs carry no steps."""
    steps = _mapping(job).get("steps")
    if not isinstance(steps, list):
        return []
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


def test_release_please_is_dispatch_only_and_pinned() -> None:
    """The bot runs on owner dispatch; ci.yml owns the push-to-main path."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    trigger = document[True] if True in document else document["on"]
    assert set(trigger) == {"workflow_dispatch"}, (
        "release-please must be dispatch-only; ci.yml runs on every push to main"
    )
    jobs = _mapping(document["jobs"])
    ci = _mapping(jobs.get("ci"))
    assert ci.get("uses") == "$/.github/workflows/ci.yml", (
        "release-please must call ci.yml first, so no tag is created on an unproven commit"
    )
    assert "id-token: write" in (WORKFLOW_PATH.read_text(encoding="utf-8")), (
        "the called coverage job needs id-token in the caller's grant"
    )
    release_please = _mapping(jobs.get("release-please"))
    assert release_please.get("needs") == ["ci"], "the release-please job must depend on ci passing"
    action = [str(step["uses"]) for job in jobs.values() for step in _steps(job) if "uses" in step]
    assert any(uses.startswith("googleapis/release-please-action@") for uses in action)
    for uses in action:
        reference = uses.rsplit("@", 1)[-1]
        assert len(reference) == 40 and all(c in "0123456789abcdef" for c in reference), (
            f"{uses} must pin a full commit SHA"
        )


def test_release_uses_an_app_token_and_no_sha_dispatch() -> None:
    """App tags fire release.yml; the old SHA-ref dispatch never could."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "GITHUB_SHA" not in text, "workflow_dispatch accepts a ref name, never a commit SHA"
    assert "actions/create-github-app-token@" in text, (
        "only an App token gives bot PRs CI checks and bot tags a push event"
    )
    assert "steps.app-token.outputs.token" in text
    assert "steps.release.outputs.pr || '{}'" in text, "an empty release output must not break fromJSON"


def test_config_lets_commit_types_choose_the_bump() -> None:
    """No manual release-as override may replace the commit-derived bump."""
    config = _config()
    assert config["release-type"] == "python"
    assert config.get("bump-minor-pre-major") is True
    assert "release-as" not in config
    for path, entry in _mapping(config["packages"]).items():
        assert "release-as" not in _mapping(entry), path


def test_no_workflow_rewrites_a_declared_version() -> None:
    """Release-please is the only writer of a package version number."""
    for path in workflow_files(SERVICE_ROOT / ".github/workflows"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in _mapping(document["jobs"]).values():
            for step in _steps(job):
                run = str(step.get("run", ""))
                assert "write_text" not in run, f"{path.name} writes a file from a step"
                assert ".dev" not in run, f"{path.name} invents a dev version"
