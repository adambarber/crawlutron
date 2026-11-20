"""The roster derives from the manifests, and "released" from the tags at HEAD."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import yaml

from scripts.release_roster import primary_tag, released, roster

RELEASE_WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
GIT = shutil.which("git") or "git"


def _git(root: Path, *arguments: str) -> None:
    """Run git pinned to ``root`` by environment, so a hook's git vars cannot redirect it.

    A test identity is set so ``git commit`` works in any environment, including CI,
    where no ``user.name``/``user.email`` is configured.
    """
    environment = {
        **os.environ,
        "GIT_DIR": str(root / ".git"),
        "GIT_WORK_TREE": str(root),
        "GIT_INDEX_FILE": str(root / ".git/index"),
        "GIT_AUTHOR_NAME": "Crawlutron Test Suite",
        "GIT_AUTHOR_EMAIL": "test@crawlutron.local",
        "GIT_COMMITTER_NAME": "Crawlutron Test Suite",
        "GIT_COMMITTER_EMAIL": "test@crawlutron.local",
    }
    subprocess.run([GIT, *arguments], cwd=root, env=environment, check=True, capture_output=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stand_up(tmp_path: Path, member_version: str = "2.0.0") -> Path:
    _write(
        tmp_path / "pyproject.toml",
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n\n[project]\nname = "app"\nversion = "1.0.0"\n',
    )
    _write(
        tmp_path / "packages/one/pyproject.toml",
        f'[project]\nname = "one"\nversion = "{member_version}"\n',
    )
    for command in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "init")):
        _git(tmp_path, *command)
    return tmp_path


def _tag(root: Path, name: str) -> None:
    _git(root, "tag", name)


def test_the_roster_derives_name_version_and_path_from_each_manifest(tmp_path: Path) -> None:
    root = _stand_up(tmp_path)
    entries = {entry["name"]: entry for entry in roster(root)}
    assert set(entries) == {"app", "one"}
    assert entries["one"]["version"] == "2.0.0"
    assert entries["one"]["path"] == "packages/one"
    assert entries["one"]["tag"] == "one-v2.0.0"
    assert entries["app"]["path"] == "."


def test_only_packages_whose_version_tag_points_at_head_are_released(tmp_path: Path) -> None:
    """A release-please tag names the exact version, so a stale tag releases nothing."""
    root = _stand_up(tmp_path)
    _tag(root, "one-v2.0.0")
    _tag(root, "app-v9.9.9")
    assert [entry["name"] for entry in released(root)] == ["one"]


def test_a_bumped_version_without_its_tag_releases_nothing(tmp_path: Path) -> None:
    """The tag and the declared version must agree; a bump alone is not a release."""
    root = _stand_up(tmp_path)
    _tag(root, "one-v2.0.0")
    text = (root / "packages/one/pyproject.toml").read_text(encoding="utf-8")
    _write(root / "packages/one/pyproject.toml", text.replace("2.0.0", "2.1.0"))
    assert tomllib.loads(text)["project"]["version"] == "2.0.0"
    assert released(root) == []


def test_the_primary_tag_is_deterministic_and_a_dispatch_owns_the_release(tmp_path: Path) -> None:
    """One of N tags at a commit publishes; a manual retry always publishes."""
    root = _stand_up(tmp_path)
    _tag(root, "one-v2.0.0")
    _tag(root, "app-v1.0.0")
    assert primary_tag(root, "push", "app-v1.0.0") is True
    assert primary_tag(root, "push", "one-v2.0.0") is False
    assert primary_tag(root, "workflow_dispatch", "main") is True
    assert primary_tag(root, "push", "not-a-release") is False


def test_tag_runs_for_one_commit_share_a_concurrency_group() -> None:
    """Two component tags at one commit must not race each other's releases."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    concurrency = document["concurrency"]
    assert concurrency["group"] == "release-${{ github.sha }}"
    assert concurrency["cancel-in-progress"] is False


def test_only_the_primary_tag_runs_the_gates_and_publishes() -> None:
    """Non-primary tag runs resolve the roster but touch no gate or index."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert jobs["released"].get("needs") in (None, [])
    assert "primary" in jobs["released"]["outputs"]
    assert "needs.released.outputs.primary == 'true'" in jobs["ci"]["if"]
    for name in (
        "publish-build",
        "publish",
        "install-graph",
        "image",
        "sbom",
        "release-artifacts",
    ):
        assert "needs.released.outputs.primary == 'true'" in jobs[name]["if"], name


def test_a_manual_retry_can_publish_an_existing_tag_from_the_fixed_workflow() -> None:
    """Dispatch takes a tag; the checkout follows it, so old tags need no moving."""
    workflow_doc = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    dispatch_triggers = workflow_doc.get("on", workflow_doc.get(True))
    tag_input = dispatch_triggers["workflow_dispatch"]["inputs"]["tag"]
    assert tag_input["type"] == "string"
    refs = [
        job["steps"][i]["with"]["ref"]
        for job in workflow_doc["jobs"].values()
        for i, step in enumerate(job.get("steps", []))
        if "with" in step and "ref" in step["with"]
    ]
    assert refs and set(refs) == {"${{ inputs.tag || github.sha }}", "${{ github.sha }}"}
    smoke = "\n".join(
        str(step.get("run", ""))
        for step in workflow_doc["jobs"]["image"]["steps"]
        if "Smoke the built image" == step.get("name")
    )
    assert "seq 1 60" in smoke
    assert "curl --noproxy '*'" in smoke, "loopback probes must bypass any HTTP proxy"
    assert 'version("crawlutron")' in smoke, "assert the root release version, not the core member"
    assert "docker inspect -f" in smoke
    released_job = workflow_doc["jobs"]["released"]
    assert released_job["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    image_job = workflow_doc["jobs"]["image"]
    assert image_job["steps"][0]["with"]["ref"] == "${{ github.sha }}"


def test_the_release_trigger_names_component_tags_not_any_dash_vee() -> None:
    """A wildcard around `-v` would fire on unrelated tags."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    triggers = document.get("on", document.get(True))
    assert set(triggers["push"]["tags"]) == {
        "v*",
        "crawlutron-v*",
        "crawlutron-core-v*",
        "crawlutron-extract-v*",
        "crawlutron-mcp-v*",
        "crawlutron-rest-v*",
        "crawlutron-safe-url-v*",
        "crawlutron-sdk-v*",
    }


def test_an_explicit_tag_resolves_the_roster_without_moving_the_checkout(tmp_path: Path) -> None:
    """A dispatch at main can publish a tag that points at an older commit."""
    root = _stand_up(tmp_path)
    assert [entry["name"] for entry in released(root, tag="one-v2.0.0")] == ["one"]


def test_the_root_declares_every_workspace_member_as_a_runtime_dependency() -> None:
    """An omitted member makes the published app unimportable."""
    manifest_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {
        str(dependency).split("[")[0].split(">=")[0].split("==")[0]
        for dependency in manifest["project"]["dependencies"]
        if str(dependency).startswith("crawlutron")
    }
    members = {entry["name"] for entry in roster() if entry["name"] != "crawlutron"}
    assert declared == members


def test_the_publish_matrix_is_the_released_set_never_a_hand_typed_list() -> None:
    """A literal name in the matrix is a package the roster does not govern."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    matrix = document["jobs"]["publish"]["strategy"]["matrix"]
    rendered = " ".join(str(value) for value in matrix.values())
    assert "fromJSON(needs.released.outputs.packages)" in rendered
    assert "fromJSON(needs.roster" not in rendered, "the roster job is the writer; never re-parse it"
