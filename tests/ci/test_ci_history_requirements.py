"""Contract tests for CI jobs that need history or config from the tree."""

from __future__ import annotations

import yaml

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
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


def test_real_pbkdf2_execution_is_one_shared_decision() -> None:
    """The PBKDF2 door call carries the shared decision; report consumers are guarded."""
    jobs = _workflow_jobs()
    changes = jobs["changes"]
    assert isinstance(changes, dict)
    outputs = changes.get("outputs")
    assert isinstance(outputs, dict)
    decision = str(outputs.get("real-pbkdf2", ""))
    assert "github.event_name != 'pull_request'" in decision
    for token in ("core-storage", "tests", "lockfile"):
        assert f"steps.filter.outputs.{token}" in decision, f"PBKDF2 decision lost {token!r}"

    producer = jobs["real-pbkdf2"]
    assert isinstance(producer, dict)
    assert "if" not in producer, "a skipped job reddens the required aggregate"
    guard = "needs.changes.outputs.real-pbkdf2 == 'true'"

    # The door step carries the PBKDF2 guard and marker.
    door_steps = [step for step in _steps(producer) if "run-tests" in str(step.get("uses", ""))]
    assert len(door_steps) == 1, "real-pbkdf2 job must call the run-tests door"
    door_step = door_steps[0]
    assert guard in str(door_step.get("if", "")), "door step lost the shared PBKDF2 decision"
    with_ = door_step.get("with", {})
    assert isinstance(with_, dict)
    assert str(with_.get("marker", "")) == "real_pbkdf2", "door must pass real_pbkdf2 marker"

    # Coverage job download/upload steps for real-pbkdf2 are also guarded.
    for step in _steps(jobs["coverage"]):
        name = str(step.get("name", ""))
        if name.startswith("Download real-pbkdf2") or name.startswith("Upload real-pbkdf2"):
            assert guard in str(step.get("if", "")), f"{name} lost the shared PBKDF2 decision"
