"""The policy model refuses malformed and stale waivers at construction."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.workflows._model import JOBS, POLICY, WORKFLOWS
from tests.workflows._policy import CEILINGS, Policy, RuleId, Waiver, refusal_literals


def test_an_unknown_rule_in_a_waiver_is_unrepresentable() -> None:
    with pytest.raises(ValidationError, match="rule"):
        Waiver(rule="xcfb", workflow="ci", job="ruff", step=None, reason="because")


def test_a_blank_waiver_reason_is_refused() -> None:
    with pytest.raises(ValidationError, match="reason"):
        Waiver(rule=RuleId.NO_BARE_PYTEST, workflow="ci", job="ruff", step=None, reason="   ")


def test_a_waiver_naming_a_missing_step_fails_at_construction() -> None:
    stale = Waiver(
        rule=RuleId.NO_BARE_PYTEST,
        workflow="ci",
        job="ruff",
        step="A step that does not exist",
        reason="historical",
    )
    ceilings = dict(CEILINGS)
    ceilings[RuleId.NO_BARE_PYTEST] = 1
    with pytest.raises(ValueError, match="stale_step"):
        Policy.create((stale,), context={"jobs": JOBS, "workflows": WORKFLOWS, "ceilings": ceilings})


def test_a_waiver_naming_a_missing_job_fails_at_construction() -> None:
    ghost = Waiver(rule=RuleId.NO_HEREDOC, workflow="ci", job="ghost", step=None, reason="historical")
    with pytest.raises(ValueError, match="stale_job"):
        Policy.create((ghost,), context={"jobs": JOBS, "workflows": WORKFLOWS})


def test_a_ceiling_mismatch_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="ceiling_drift"):
        Policy.create(
            (),
            context={
                "jobs": JOBS,
                "workflows": WORKFLOWS,
                "ceilings": {**CEILINGS, RuleId.NO_BARE_PYTEST: 5},
            },
        )


def test_a_partial_ceiling_table_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="missing_ceiling"):
        Policy.create(
            (), context={"jobs": JOBS, "workflows": WORKFLOWS, "ceilings": {RuleId.NO_BARE_PYTEST: 0}}
        )


def test_the_shipped_policy_is_admitted() -> None:
    assert POLICY.waivers
    assert set(POLICY.ceilings) == set(RuleId)
    assert "stale_step" in refusal_literals()


def test_a_raw_waiver_cannot_be_validated_into_a_policy() -> None:
    from pydantic import TypeAdapter

    with pytest.raises(TypeError, match="admitted by construction"):
        TypeAdapter(Policy).validate_python({"waivers": []})
