"""The workflow model parses the whole surface and fails closed on bad values."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.workflows._model import (
    JOBS,
    PARSE_COUNT,
    WORKFLOWS,
    Job,
    Step,
    workflow,
)


def test_every_workflow_on_disk_parses_exactly_once() -> None:
    assert len(WORKFLOWS) == 11
    assert PARSE_COUNT == len(WORKFLOWS)
    assert len(JOBS) == sum(len(item.jobs) for item in WORKFLOWS)
    assert all(step.line > 0 for job in JOBS for step in job.steps)
    assert workflow("release").stem == "release"


def test_a_call_job_has_no_steps_and_a_runner_job_has_no_uses() -> None:
    assert all(bool(job.uses) != bool(job.steps) for job in JOBS)


def test_truthy_continue_on_error_is_unparseable_unless_waived() -> None:
    base = {"name": "Emit", "run": "true", "line": 1}
    with pytest.raises(ValidationError, match="continue-on-error"):
        Step.model_validate({**base, "continue-on-error": True})
    with pytest.raises(ValidationError, match="continue-on-error"):
        Step.model_validate({**base, "continue-on-error": "${{ true }}"})


def test_the_only_truthy_continue_on_error_in_the_tree_is_the_waived_guard() -> None:
    waived = [step for job in JOBS for step in job.steps if step.continue_on_error is True]
    assert len(waived) == 1
    assert waived[0].name == "Create GitHub App token"


def test_a_job_with_both_uses_and_steps_is_unparseable() -> None:
    with pytest.raises(ValidationError, match="either calls"):
        Job.model_validate(
            {
                "key": "broken",
                "workflow": "ci",
                "line": 1,
                "uses": "$/.github/workflows/ci.yml",
                "steps": [{"run": "true", "line": 2}],
            }
        )
