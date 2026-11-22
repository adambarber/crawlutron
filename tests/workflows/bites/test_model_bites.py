"""Every model constraint is proved to reject its violation."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.workflows._model import Job, Step, Workflow, workflow
from tests.workflows._names import ActionRef, EnvName, Expression, StepName


@pytest.mark.parametrize(
    ("adapter", "value", "token"),
    [
        (TypeAdapter(StepName), "   ", "at least 1 character"),
        (TypeAdapter(StepName), "Use ${{ github.event.x }}", "interpolate"),
        (TypeAdapter(ActionRef), "actions/checkout@v4", "40-hex"),
        (TypeAdapter(EnvName), "bad-name", "UPPER_SNAKE"),
        (TypeAdapter(Expression), "${{ unbalanced", "braces"),
    ],
)
def test_a_typed_primitive_names_its_violation(adapter, value, token) -> None:
    with pytest.raises(ValidationError, match=token):
        adapter.validate_python(value)


def test_a_truthy_continue_on_error_is_rejected_without_a_waiver() -> None:
    with pytest.raises(ValidationError, match="continue-on-error"):
        Step.model_validate({"name": "Bite", "run": "true", "continue-on-error": "true", "line": 7})


def test_a_call_job_with_steps_is_rejected() -> None:
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


def test_an_unknown_github_key_fails_closed() -> None:
    raw = workflow("ci").model_dump(mode="json")
    raw["invented-key"] = "value"
    with pytest.raises(ValidationError, match="invented-key"):
        Workflow.model_validate(raw)
