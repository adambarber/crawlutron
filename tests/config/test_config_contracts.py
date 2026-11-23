"""Live config satisfies every typed config rule, and every rule is registered."""

from __future__ import annotations

import pytest

from tests.config._policy import RuleId
from tests.config._rules import LIVE, RULES


def test_every_policy_id_has_a_rule() -> None:
    assert {rule.id for rule in RULES} == set(RuleId), "a policy id has no check"


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.id.value)
def test_live_config_has_no_findings(rule) -> None:
    findings = list(rule.check(LIVE))
    assert findings == [], [finding.message for finding in findings]


def test_publish_matrices_remain_serial_and_idempotent() -> None:
    """Publishers stay max-parallel 1 with skip-existing so retries are safe."""
    publishers = [
        job
        for item in LIVE.workflows
        for job in item.jobs.values()
        if any(step.uses and "gh-action-pypi-publish" in step.uses for step in job.steps)
        and job.strategy
        and any("released.outputs.packages" in str(value) for value in job.strategy.matrix.values())
    ]
    assert publishers
    for job in publishers:
        assert job.strategy is not None and job.strategy.max_parallel == 1
        publish_steps = [step for step in job.steps if step.uses and "gh-action-pypi-publish" in step.uses]
        assert any(step.with_.get("skip-existing") is True for step in publish_steps)
