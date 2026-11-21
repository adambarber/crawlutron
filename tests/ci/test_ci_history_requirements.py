"""Contract tests for CI jobs that need history or config from the tree."""

from __future__ import annotations

from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"
CONFIG_PATH = SERVICE_ROOT / ".github" / "dependency-review-config.yml"


def _workflow_jobs() -> dict[str, object]:
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: object) -> list[dict[str, object]]:
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _checkouts(job: object) -> list[dict[str, object]]:
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith("actions/checkout@")]


def test_secret_scan_fetches_full_history() -> None:
    checkouts = _checkouts(_workflow_jobs()["secret-scan"])
    assert len(checkouts) == 1
    options = checkouts[0].get("with")
    assert isinstance(options, dict)
    assert str(options.get("fetch-depth")) == "0"


def test_dependency_review_checks_out_config_source() -> None:
    assert len(_checkouts(_workflow_jobs()["dependency-review"])) == 1


def test_dependency_review_supplies_refs_on_push_events() -> None:
    steps = _steps(_workflow_jobs()["dependency-review"])
    review = [step for step in steps if "dependency-review-action@" in str(step.get("uses", ""))]
    assert len(review) == 1
    options = review[0].get("with")
    assert isinstance(options, dict)
    assert "github.event.before" in str(options.get("base-ref", ""))
    assert "github.sha" in str(options.get("head-ref", ""))


def test_dependency_review_config_is_tracked() -> None:
    assert CONFIG_PATH.is_file()


def test_real_pbkdf2_job_is_never_skipped_by_a_path_filter() -> None:
    """The required gate must see success, never skipped, when the surface is unfiltered."""
    job = _workflow_jobs()["real-pbkdf2"]
    assert isinstance(job, dict)
    assert "if" not in job, "a skipped job reddens the required aggregate"
    runs = [step for step in _steps(job) if 'pytest -m "real_pbkdf2"' in str(step.get("run", ""))]
    assert len(runs) == 1
    condition = str(runs[0].get("if", ""))
    for token in (
        "github.event_name != 'pull_request'",
        "needs.changes.outputs.core-storage",
        "needs.changes.outputs.tests",
        "needs.changes.outputs.lockfile",
    ):
        assert token in condition, f"the real-pbkdf2 step filter lost {token!r}"
