"""Every model constraint is proved to reject its violation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.workflows._admission import admit_or_none
from tests.workflows._facts import CommandFacts
from tests.workflows._model import JOBS, Job, Step, Workflow, parse_workflow, workflow
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
        Step.create({"name": "Bite", "run": "true", "continue-on-error": "true", "line": 7})


def test_a_runner_job_without_a_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timeout-minutes"):
        Job.create(
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
        Workflow.create(payload)
    payload["on"] = {"schedule": [{"cron": "61 99 * * MONDAY"}]}
    with pytest.raises(ValidationError, match="cron range|cron name"):
        Workflow.create(payload)
    for invalid in ("*/0 * * * *", "1/0 * * * *"):
        payload["on"] = {"schedule": [{"cron": invalid}]}
        with pytest.raises(ValidationError, match="cron step"):
            Workflow.create(payload)


def test_an_unknown_workflow_trigger_is_rejected() -> None:
    payload = {
        "stem": "ghost",
        "path": Path("ghost.yml"),
        "name": "Ghost",
        "on": {"ghost_trigger": None},
        "jobs": {
            "j": {
                "key": "j",
                "workflow": "ghost",
                "line": 1,
                "uses": "$/.github/workflows/ci.yml",
            }
        },
    }
    with pytest.raises(ValidationError, match="unknown workflow trigger"):
        Workflow.create(payload)


def test_admit_or_none_collapses_only_refusals() -> None:
    def refuse(_: object) -> Step:
        raise ValueError("refused")

    def explode(_: object) -> Step:
        raise RuntimeError("foreign")

    assert admit_or_none(refuse, 1) is None
    with pytest.raises(RuntimeError):
        admit_or_none(explode, 1)
    assert Step.admit_or_none({"name": "Bite", "run": "true", "continue-on-error": True, "line": 1}) is None


def test_copying_a_waived_step_keeps_its_admission() -> None:
    waived = next(step for job in JOBS for step in job.steps if step.continue_on_error)
    copied = waived.model_copy(update={"name": waived.name})
    assert copied.continue_on_error is True


def test_a_step_cannot_be_constructed_without_validation() -> None:
    with pytest.raises(TypeError, match="admitted by validation"):
        Step.model_construct(name="x", run="pip install x", line=1)


def test_facts_are_computed_once_per_step(monkeypatch) -> None:
    seen: list[str] = []
    original = CommandFacts.parse

    def counted(run: str, env=None) -> CommandFacts:
        seen.append(run)
        return original(run, env)

    monkeypatch.setattr(CommandFacts, "parse", counted)
    step = Step.create({"name": "Once", "run": "echo hi", "line": 1})
    assert step.command_facts == step.command_facts
    assert len(seen) == 1


def test_a_call_job_with_steps_is_rejected() -> None:
    with pytest.raises(ValidationError, match="either calls"):
        Job.create(
            {
                "key": "broken",
                "workflow": "ci",
                "line": 1,
                "uses": "$/.github/workflows/ci.yml",
                "steps": [{"run": "true", "line": 2}],
            }
        )


def test_a_hyphenated_codecov_input_is_refused() -> None:
    with pytest.raises(ValidationError, match="codecov input"):
        Step.create(
            {
                "name": "Upload",
                "uses": "codecov/codecov-action@" + "a" * 40,
                "with": {"report-type": "test_results"},
                "line": 7,
            }
        )


def test_an_unknown_github_key_fails_closed() -> None:
    raw = workflow("ci").model_dump(mode="json")
    raw["invented-key"] = "value"
    with pytest.raises(ValidationError, match="invented-key"):
        Workflow.create(raw)
