"""Admitted policy: ceilings per rule and waivers that name live Makefile targets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, ConfigDict


@runtime_checkable
class TargetLike(Protocol):
    key: str


STRICT = ConfigDict(extra="forbid", frozen=True)


def _reason(value: str) -> str:
    if not value.strip() or len(value) > 160:
        raise ValueError("blank_reason")
    return value.strip()


Reason = Annotated[str, AfterValidator(_reason)]


class RuleId(StrEnum):
    MAKEFILE_HOOK_IDS_RESOLVE = "makefile-hook-ids-resolve"
    MAKEFILE_NO_GATE_RESTATED = "makefile-no-gate-restated"
    CI_REQUIRED_AGGREGATE = "ci-required-aggregate"
    JSCPD_SINGLE_HOOK = "jscpd-single-hook"


class Waiver(BaseModel):
    model_config = STRICT
    rule: RuleId
    target: str
    step: str | None = None
    reason: Reason

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Waiver:
        return cls.model_validate(raw)


WAIVERS: tuple[Waiver, ...] = ()

CEILINGS: Mapping[RuleId, int] = MappingProxyType(
    {
        RuleId.MAKEFILE_HOOK_IDS_RESOLVE: 0,
        RuleId.MAKEFILE_NO_GATE_RESTATED: 0,
        RuleId.CI_REQUIRED_AGGREGATE: 0,
        RuleId.JSCPD_SINGLE_HOOK: 0,
    }
)


class Policy:
    """Admitted waiver surface: every exemption names a live target; every rule has a ceiling."""

    __slots__ = ("_ceilings", "_waivers")

    def __init__(
        self,
        waivers: tuple[Waiver, ...],
        targets: Sequence[TargetLike],
        *,
        ceilings: Mapping[RuleId, int] = CEILINGS,
    ) -> None:
        try:
            self._admit(waivers, targets, ceilings)
        except ValueError as error:
            raise ValueError(_refusal_literal(str(error))) from error
        self._waivers = tuple(waivers)
        self._ceilings = MappingProxyType(dict(ceilings))

    @staticmethod
    def _admit(
        waivers: tuple[Waiver, ...],
        targets: Sequence[TargetLike],
        ceilings: Mapping[RuleId, int],
    ) -> None:
        if not targets:
            raise ValueError("empty_tree")
        if set(ceilings) != set(RuleId):
            raise ValueError("missing_ceiling")
        target_keys = {str(item.key) for item in targets}
        for waiver in waivers:
            if waiver.target not in target_keys:
                raise ValueError("stale_target")
        for rule in RuleId:
            if sum(item.rule is rule for item in waivers) != ceilings[rule]:
                raise ValueError("ceiling_drift")

    @classmethod
    def create(
        cls,
        raw: object,
        *,
        context: Mapping[str, object] | None = None,
    ) -> Policy:
        context = context or {}
        targets = context["targets"]
        if not isinstance(targets, Sequence):
            raise ValueError("empty_tree")
        ceilings = context.get("ceilings", CEILINGS)
        if not isinstance(raw, tuple) or not all(isinstance(item, Waiver) for item in raw):
            raise ValueError("invalid_waivers")
        if not isinstance(ceilings, Mapping) or set(ceilings) != set(RuleId):
            raise ValueError("missing_ceiling")
        return cls(raw, targets, ceilings=ceilings)

    def is_waived(self, target: str, step: str | None, rule: RuleId) -> bool:
        return any(
            item.rule is rule and item.target == target and item.step == step for item in self._waivers
        )

    def count(self, rule: RuleId) -> int:
        return sum(item.rule is rule for item in self._waivers)

    @property
    def waivers(self) -> tuple[Waiver, ...]:
        return self._waivers

    @property
    def ceilings(self) -> Mapping[RuleId, int]:
        return self._ceilings


def _refusal_literal(reason: str) -> str:
    _REFUSALS = frozenset(
        {"missing_ceiling", "ceiling_drift", "stale_target", "empty_tree", "invalid_waivers"}
    )
    return reason if reason in _REFUSALS else "ceiling_drift"
