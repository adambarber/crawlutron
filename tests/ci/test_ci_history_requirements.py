"""Residual contract tests for CI jobs not covered by the typed workflow rules."""

from __future__ import annotations

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"
CONFIG_PATH = SERVICE_ROOT / ".github" / "dependency-review-config.yml"


def _workflow_jobs() -> dict[str, object]:
    import yaml

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


def test_dependency_review_config_is_tracked() -> None:
    """The dependency-review config file must be tracked in the tree."""
    assert CONFIG_PATH.is_file()


def test_real_pkdf2_execution_uses_the_shared_door() -> None:
    """The PBKDF2 job always calls the shared door (no step-level if:) and
    report consumers stay guarded."""
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

    door_steps = [step for step in _steps(producer) if "run-tests" in str(step.get("uses", ""))]
    assert len(door_steps) == 1, "real-pbkdf2 job must call the run-tests door"
    door_step = door_steps[0]
    assert "if" not in door_step, "door step must not carry a step-level if"
    with_ = door_step.get("with", {})
    assert isinstance(with_, dict)
    assert str(with_.get("marker", "")) == "real_pbkdf2", "door must pass real_pbkdf2 marker"

    for step in _steps(jobs["coverage"]):
        name = str(step.get("name", ""))
        if name.startswith("Download real-pbkdf2") or name.startswith("Upload real-pbkdf2"):
            guard = "needs.changes.outputs.real-pbkdf2 == 'true'"
            assert guard in str(step.get("if", "")), f"{name} lost the shared PBKDF2 decision"
