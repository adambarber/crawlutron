"""Every model constraint is proved to reject its violation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.workflows._model import Job, Step, Workflow, parse_workflow, workflow
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


def test_a_runner_job_without_a_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timeout-minutes"):
        Job.model_validate(
            {
                "key": "broken",
                "workflow": "ci",
                "line": 1,
                "runs-on": "ubuntu-24.04",
                "steps": [{"run": "true", "line": 2}],
            }
        )


def test_an_author_set_waiver_is_refused(tmp_path) -> None:
    path = tmp_path / "ghost.yml"
    path.write_text(
        "name: Ghost\non: push\npermissions: {}\n"
        "jobs:\n  j:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 1\n"
        "    steps:\n      - name: Sneaky\n        run: 'true'\n"
        "        continue-on-error: true\n        waived_continue_on_error: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="author-controlled"):
        parse_workflow(path)


def test_a_bad_schedule_cron_is_rejected() -> None:
    payload = {
        "stem": "ghost",
        "path": Path("ghost.yml"),
        "name": "Ghost",
        "on": {"schedule": [{"cron": "every five minutes"}]},
        "jobs": {
            "j": {
                "key": "j",
                "workflow": "ghost",
                "line": 1,
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 1,
                "steps": [{"run": "true", "line": 2}],
            }
        },
    }
    with pytest.raises(ValidationError, match="cron"):
        Workflow.model_validate(payload)


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
