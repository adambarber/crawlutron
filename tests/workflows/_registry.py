"""Typed decision registration. The catalog is derived, never hand-written."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, TypeVar

from pydantic import AfterValidator, validate_call

from tests.workflows._model import WORKFLOWS

_F = TypeVar("_F", bound=Callable[..., object])


class Decision(StrEnum):
    FULL_MARKER_SET = "full-marker-set"
    OIDC_AT_PUBLISH_ONLY = "oidc-at-publish-only"
    COVERAGE_NO_RERUN = "coverage-no-rerun"
    NO_WRITE_SCOPES = "no-write-scopes"
    ROSTER_OUTPUTS = "roster-outputs"
    REUSABLE_ONLY = "reusable-only"
    DISPATCH_ONLY = "dispatch-only"
    TWO_PLANE_TRUST = "two-plane-trust"
    SECURITY_EVENTS_WRITE = "security-events-write"
    SCHEDULED_ANALYSIS = "scheduled-analysis"
    MUTATION_TIMEBOX = "mutation-timebox"


def _known_workflow(value: str) -> str:
    stems = sorted(item.stem for item in WORKFLOWS)
    if value not in stems:
        raise ValueError(f"unknown workflow {value!r}; known: {stems}")
    return value


WorkflowRef = Annotated[str, AfterValidator(_known_workflow)]
CatalogEntry = tuple[Decision, tuple[str, ...]]
CATALOG: dict[str, set[CatalogEntry]] = {}


@validate_call
def decision(workflow: WorkflowRef, kind: Decision, *, jobs: tuple[str, ...] = ()) -> Callable[[_F], _F]:
    """Bind a named decision to a workflow; both axes are validated at import."""

    def register(function: _F) -> _F:
        CATALOG.setdefault(workflow, set()).add((kind, jobs))
        return function

    return register
