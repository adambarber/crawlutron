"""The policy model refuses malformed and stale waivers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.workflows._model import JOBS, WORKFLOWS
from tests.workflows._policy import POLICY, Policy, RuleId, Waiver


def test_an_unknown_rule_in_a_waiver_is_unrepresentable() -> None:
    with pytest.raises(ValidationError, match="rule"):
        Waiver(rule="xcfb", workflow="ci", job="ruff", step=None, reason="because")


def test_a_blank_waiver_reason_is_refused() -> None:
    with pytest.raises(ValidationError, match="reason"):
        Waiver(
            rule=RuleId.NO_BARE_PYTEST,
            workflow="ci",
            job="ruff",
            step=None,
            reason="   ",
        )


def test_a_waiver_naming_a_missing_step_fails_closed() -> None:
    stale = Policy(
        waivers=(
            Waiver(
                rule=RuleId.NO_BARE_PYTEST,
                workflow="ci",
                job="ruff",
                step="A step that does not exist",
                reason="historical",
            ),
        ),
        ceilings={RuleId.NO_BARE_PYTEST: 1},
    )
    with pytest.raises(AssertionError, match="missing step"):
        stale.validate_targets(WORKFLOWS, JOBS)


def test_the_shipped_policy_validates_against_the_live_tree() -> None:
    POLICY.validate_targets(WORKFLOWS, JOBS)
