"""Admitted policy: constructing one IS admission against the live workflow tree."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, ConfigDict

from tests.workflows._admission import admit_or_none as _admit_or_none
from tests.workflows._names import JobId, StepName, WorkflowStem


@runtime_checkable
class StepLike(Protocol):
    name: str | None


@runtime_checkable
class JobLike(Protocol):
    workflow: str
    key: str
    steps: tuple[StepLike, ...]


@runtime_checkable
class WorkflowLike(Protocol):
    stem: str


STRICT = ConfigDict(extra="forbid", frozen=True)

_REFUSALS = frozenset(
    {
        "missing_ceiling",
        "ceiling_drift",
        "stale_job",
        "stale_step",
        "empty_tree",
        "invalid_waivers",
    }
)


def _reason(value: str) -> str:
    if not value.strip() or len(value) > 160:
        raise ValueError("blank_reason")
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
    TESTS_USE_DOOR = "tests-use-door"
    DOOR_PROOF_CONSUMED = "door-proof-consumed"


class Waiver(BaseModel):
    model_config = STRICT
    rule: RuleId
    workflow: WorkflowStem
    job: JobId
    step: StepName | None
    reason: Reason

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Waiver:
        return cls.model_validate(raw)

    @classmethod
    def admit_or_none(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Waiver | None:
        return _admit_or_none(lambda value: cls.create(value), raw)


WAIVERS: tuple[Waiver, ...] = (
    Waiver(
        rule=RuleId.CONTINUE_ON_ERROR,
        workflow="agent-review",
        job="review",
        step="Create GitHub App token",
        reason="A fail-closed guard step follows; the token is advisory, not the gate",
    ),
    Waiver(
        rule=RuleId.NO_PIP_INSTALL,
        workflow="weekly",
        job="lowest-direct-resolution",
        step="Install lowest-direct dependencies",
        reason="The lowest-direct probe must resolve against an older graph on purpose",
    ),
)

CEILINGS: Mapping[RuleId, int] = MappingProxyType(
    {
        RuleId.CONTINUE_ON_ERROR: 1,
        RuleId.NO_EVENT_SPLICING: 0,
        RuleId.NO_BARE_PYTEST: 0,
        RuleId.NO_PIP_INSTALL: 1,
        RuleId.NO_HEREDOC: 0,
        RuleId.NEEDS_RESOLVE: 0,
        RuleId.UNIQUE_STEP_IDS: 0,
        RuleId.DECLARES_PERMISSIONS: 0,
        RuleId.HAS_TRIGGER: 0,
        RuleId.TESTS_USE_DOOR: 0,
        RuleId.DOOR_PROOF_CONSUMED: 0,
    }
)


def waived_continue_on_error(workflow: str, job: str, step: str | None) -> bool:
    """Parse-door admission for one step; no tree needed, no Policy instance needed."""
    return any(
        item.rule is RuleId.CONTINUE_ON_ERROR
        and item.workflow == workflow
        and item.job == job
        and item.step == step
        for item in WAIVERS
    )


class Policy:
    """An admitted waiver surface: every exemption names a live target and every
    rule has a ceiling. No public constructor path skips admission."""

    __slots__ = ("_ceilings", "_waivers")

    def __init__(
        self,
        waivers: tuple[Waiver, ...],
        jobs: Sequence[JobLike],
        workflows: Sequence[WorkflowLike],
        *,
        ceilings: Mapping[RuleId, int] = CEILINGS,
    ) -> None:
        try:
            self._admit(waivers, jobs, workflows, ceilings)
        except ValueError as error:
            raise ValueError(_refusal_literal(str(error))) from error
        self._waivers = tuple(waivers)
        self._ceilings = MappingProxyType(dict(ceilings))

    @staticmethod
    def _admit(
        waivers: tuple[Waiver, ...],
        jobs: Sequence[JobLike],
        workflows: Sequence[WorkflowLike],
        ceilings: Mapping[RuleId, int],
    ) -> None:
        if not workflows:
            raise ValueError("empty_tree")
        if set(ceilings) != set(RuleId):
            raise ValueError("missing_ceiling")
        for waiver in waivers:
            job = next(
                (item for item in jobs if item.workflow == waiver.workflow and item.key == waiver.job), None
            )
            if job is None:
                raise ValueError("stale_job")
            if waiver.step is not None and not any(step.name == waiver.step for step in job.steps):
                raise ValueError("stale_step")
        for rule in RuleId:
            if sum(item.rule is rule for item in waivers) != ceilings[rule]:
                raise ValueError("ceiling_drift")

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Policy:
        context = context or {}
        raw_jobs, raw_workflows = context["jobs"], context["workflows"]
        if not isinstance(raw_jobs, Sequence) or not isinstance(raw_workflows, Sequence):
            raise ValueError("empty_tree")
        jobs = tuple(item for item in raw_jobs if isinstance(item, JobLike))
        workflows = tuple(item for item in raw_workflows if isinstance(item, WorkflowLike))
        if len(jobs) != len(raw_jobs) or len(workflows) != len(raw_workflows):
            raise ValueError("empty_tree")
        ceilings = context.get("ceilings", CEILINGS)
        if not isinstance(raw, tuple) or not all(isinstance(item, Waiver) for item in raw):
            raise ValueError("invalid_waivers")
        if not isinstance(ceilings, Mapping) or set(ceilings) != set(RuleId):
            raise ValueError("missing_ceiling")
        return cls(raw, jobs, workflows, ceilings=ceilings)

    @classmethod
    def admit_or_none(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Policy | None:
        return _admit_or_none(lambda value: cls.create(value, context=context), raw)

    def is_waived(self, workflow: str, job: str, step: str | None, rule: RuleId) -> bool:
        return any(
            item.rule is rule and item.workflow == workflow and item.job == job and item.step == step
            for item in self._waivers
        )

    def count(self, rule: RuleId) -> int:
        return sum(item.rule is rule for item in self._waivers)

    @property
    def waivers(self) -> tuple[Waiver, ...]:
        return self._waivers

    @property
    def ceilings(self) -> Mapping[RuleId, int]:
        return self._ceilings

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, handler):
        """Accept only an already-admitted Policy; raw waivers have no tree."""
        from pydantic_core import core_schema

        def _validate(value: object) -> Policy:
            if isinstance(value, cls):
                return value
            raise TypeError(f"{cls.__name__} is admitted by construction, not validated")

        return core_schema.no_info_plain_validator_function(_validate)


def _refusal_literal(reason: str) -> str:
    return reason if reason in _REFUSALS else "ceiling_drift"


def refusal_literals() -> frozenset[str]:
    return frozenset(_REFUSALS)
