"""Every job is governed by the same rule surface, with no silent skips."""

from __future__ import annotations

import pytest

from tests.workflows._model import JOBS, WORKFLOWS
from tests.workflows._rules import CASES, JOB_RULES, WF_CASES, WORKFLOW_RULES


def test_every_job_has_at_least_one_governing_rule() -> None:
    for job in JOBS:
        assert [rule for rule in JOB_RULES if rule.scope.matches(job)], (
            f"{job.workflow}:{job.key} is governed by no rule"
        )


def test_every_workflow_has_at_least_one_governing_rule() -> None:
    assert WORKFLOW_RULES
    assert len(WF_CASES) == len(WORKFLOWS) * len(WORKFLOW_RULES)


@pytest.mark.parametrize(("workflow", "job", "rule"), CASES)
def test_job_rule(workflow, job, rule) -> None:
    findings = list(rule.check(workflow, job))
    assert not findings, "\n".join(f.message for f in findings)


@pytest.mark.parametrize(("workflow", "rule"), WF_CASES)
def test_workflow_rule(workflow, rule) -> None:
    findings = list(rule.check(workflow))
    assert not findings, "\n".join(f.message for f in findings)
