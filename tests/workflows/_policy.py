"""Typed policy: one waiver table, validated rules, and a ratchet."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from tests.workflows._names import JobId, StepName, WorkflowStem

STRICT = ConfigDict(extra="forbid", frozen=True)


def _reason(value: str) -> str:
    if not value.strip() or len(value) > 160:
        raise ValueError("a waiver reason must be 1-160 characters")
    return value.strip()


Reason = Annotated[str, AfterValidator(_reason)]


class RuleId(StrEnum):
    CONTINUE_ON_ERROR = "continue-on-error"
    NO_EVENT_SPLICING = "no-event-splicing-in-run"
    NO_BARE_PYTEST = "no-bare-pytest"
    NO_PIP_INSTALL = "no-pip-install"
    NO_HEREDOC = "no-heredoc"
    NEEDS_RESOLVE = "needs-resolve"
    UNIQUE_STEP_IDS = "unique-step-ids"
    DECLARES_PERMISSIONS = "declares-permissions"
    HAS_TRIGGER = "has-trigger"


class Waiver(BaseModel):
    model_config = STRICT
    rule: RuleId
    workflow: WorkflowStem
    job: JobId
    step: StepName | None
    reason: Reason


class Policy(BaseModel):
    model_config = STRICT
    waivers: tuple[Waiver, ...]
    ceilings: Mapping[RuleId, int]

    def is_waived(self, workflow: str, job: str, step: str | None, rule: RuleId) -> bool:
        return any(
            item.rule is rule and item.workflow == workflow and item.job == job and item.step == step
            for item in self.waivers
        )

    def count(self, rule: RuleId) -> int:
        return sum(item.rule is rule for item in self.waivers)

    def validate_targets(self, workflows, jobs) -> None:
        """Every waiver must name a live rule target; a stale waiver fails closed."""
        names = {(job.workflow, job.key) for job in jobs}
        for waiver in self.waivers:
            assert (waiver.workflow, waiver.job) in names, (
                f"waiver names missing job {waiver.workflow}:{waiver.job}"
            )
            job = next(job for job in jobs if job.workflow == waiver.workflow and job.key == waiver.job)
            if waiver.step is not None:
                assert any(step.name == waiver.step for step in job.steps), (
                    f"waiver names missing step {waiver.workflow}:{waiver.job}:{waiver.step}"
                )
        assert {rule: self.count(rule) for rule in self.ceilings} == dict(self.ceilings)
        assert workflows


POLICY = Policy(
    waivers=(
        Waiver(
            rule=RuleId.CONTINUE_ON_ERROR,
            workflow="agent-review",
            job="review",
            step="Create GitHub App token",
            reason="A fail-closed guard step follows; the token is advisory, not the gate",
        ),
        Waiver(
            rule=RuleId.NO_BARE_PYTEST,
            workflow="ci",
            job="test-browser",
            step="Run pytest (all markers, main push and release calls)",
            reason="The main-push browser job intentionally runs the full suite",
        ),
        Waiver(
            rule=RuleId.NO_BARE_PYTEST,
            workflow="nightly",
            job="test",
            step="Run full test suite (no -m filter) with coverage",
            reason="The nightly exists to run the full unfiltered suite",
        ),
        Waiver(
            rule=RuleId.NO_BARE_PYTEST,
            workflow="weekly",
            job="mutation-testing",
            step="Run mutation testing on invariant-critical modules",
            reason="Mutation testing selects its own targets, not the battery",
        ),
        Waiver(
            rule=RuleId.NO_PIP_INSTALL,
            workflow="weekly",
            job="lowest-direct-resolution",
            step="Install lowest-direct dependencies",
            reason="The lowest-direct probe must resolve against an older graph on purpose",
        ),
    ),
    ceilings={
        RuleId.CONTINUE_ON_ERROR: 1,
        RuleId.NO_BARE_PYTEST: 3,
        RuleId.NO_PIP_INSTALL: 1,
    },
)
