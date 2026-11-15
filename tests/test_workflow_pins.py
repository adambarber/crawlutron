"""Stale CI pins and the moving runner label are refused."""

from __future__ import annotations

import yaml

from tests._ci_contract import PREPARE_ACTION_PATH, SERVICE_ROOT, WORKFLOW_PATH

RELEASE_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "release.yml"
PACKAGING_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "packaging.yml"

# Stale SHAs #1067 removed, with the tag each resolved to: setup-python v5,
# setup-uv v5, cache v4, gitleaks v2.
STALE_SHAS = (
    "a26af69be951a213d495a4c3e4e4022e16d87065",
    "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
    "0057852bfaa89a56745cba8c7296529d2fc39830",
    "ff98106e4c7b2bc287b24eaf42907196329070c7",
)

CI_SURFACE = (
    WORKFLOW_PATH,
    RELEASE_WORKFLOW_PATH,
    PACKAGING_WORKFLOW_PATH,
    PREPARE_ACTION_PATH,
)

WORKFLOW_FILES = (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH, PACKAGING_WORKFLOW_PATH)


def _surface_texts() -> list[tuple[str, str]]:
    """Each file on the CI surface as (file name, text)."""
    return [(path.name, path.read_text(encoding="utf-8")) for path in CI_SURFACE]


def test_no_ubuntu_latest_runner_label() -> None:
    """ubuntu-latest migrates; no job may ride the moving label."""
    for name, text in _surface_texts():
        assert "ubuntu-latest" not in text, f"{name} still uses ubuntu-latest"


def test_no_stale_action_pin() -> None:
    """Every `uses:` keeps the converged SHA; a stale pin must not return."""
    for name, text in _surface_texts():
        for sha in STALE_SHAS:
            assert f"@{sha}" not in text, f"{name} re-pins the stale SHA {sha}"


def test_every_job_pins_ubuntu_24_04() -> None:
    """Every job names ubuntu-24.04 as a stated fact."""
    for path in WORKFLOW_FILES:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a workflow mapping"
        jobs = document.get("jobs")
        assert isinstance(jobs, dict), f"{path.name} declares no jobs"
        for name, job in jobs.items():
            assert isinstance(job, dict), f"job {name} in {path.name} is not a mapping"
            assert job.get("runs-on") == "ubuntu-24.04", (
                f"job {name} in {path.name} does not pin ubuntu-24.04"
            )
