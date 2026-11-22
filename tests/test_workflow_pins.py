"""Workflow pins and runner labels obey the one version registry."""

from __future__ import annotations

from pathlib import Path

import yaml

import scripts.pin_verify as pv
from tests._ci_contract import PREPARE_ACTION_PATH, SERVICE_ROOT, workflow_files

WORKFLOW_SURFACE = workflow_files(SERVICE_ROOT / ".github" / "workflows")
CI_SURFACE = (*WORKFLOW_SURFACE, PREPARE_ACTION_PATH)


def _surface_texts() -> list[tuple[str, str]]:
    """Each file on the CI surface as (file name, text)."""
    return [(path.name, path.read_text(encoding="utf-8")) for path in CI_SURFACE]


def test_workflow_surface_is_every_file_on_disk() -> None:
    from tests.workflows._model import WORKFLOWS

    files = workflow_files(SERVICE_ROOT / ".github" / "workflows")
    assert len(files) == len(WORKFLOWS), f"{len(files)} files, {len(WORKFLOWS)} parsed"
    assert {path.stem for path in files} == {item.stem for item in WORKFLOWS}


def test_workflow_files_include_the_yaml_extension(tmp_path: Path) -> None:
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "one.yml").write_text("name: One\n", encoding="utf-8")
    (directory / "two.yaml").write_text("name: Two\n", encoding="utf-8")
    assert {path.name for path in workflow_files(directory)} == {"one.yml", "two.yaml"}


def test_no_ubuntu_latest_runner_label() -> None:
    """ubuntu-latest migrates; no job may ride the moving label."""
    for name, text in _surface_texts():
        assert "ubuntu-latest" not in text, f"{name} still uses ubuntu-latest"


def test_every_job_pins_ubuntu_24_04() -> None:
    """Every job names ubuntu-24.04 as a stated fact."""
    for path in WORKFLOW_SURFACE:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a workflow mapping"
        jobs = document.get("jobs")
        assert isinstance(jobs, dict), f"{path.name} declares no jobs"
        for name, job in jobs.items():
            assert isinstance(job, dict), f"job {name} in {path.name} is not a mapping"
            if job.get("uses"):
                continue
            assert job.get("runs-on") == "ubuntu-24.04", (
                f"job {name} in {path.name} does not pin ubuntu-24.04"
            )


def test_every_workflow_pin_matches_the_one_registry() -> None:
    """Every uses: ref equals its single sanctioned version and sha."""
    errors = pv.verify_pins()
    assert not errors, "registry drift:\n" + "\n".join(errors)


def test_registry_declares_one_sanctioned_version_per_dependency() -> None:
    """A dependency has exactly one entry; a second version is a design defect."""
    registry = pv.load_registry()
    assert registry, "the registry is empty"
    for dependency, pin in registry.items():
        assert pv.pin_version(pin), f"{dependency} has no version"
        assert pv.SHA.fullmatch(pv.pin_sha(pin)), f"{dependency} has no commit sha"
