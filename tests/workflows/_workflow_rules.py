"""Workflow-scoped rules and the registry that groups them."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from tests.workflows._ci_rules import (
    ci_packaging_read_only,
    ci_release_gated_on_ci,
    ci_security_permissions,
)
from tests.workflows._codeql_rules import codeql_triggers
from tests.workflows._model import Workflow
from tests.workflows._nightly_rules import (
    nightly_artifacts,
    nightly_concurrency,
    nightly_name,
    nightly_triggers,
)
from tests.workflows._policy import Finding, RuleId
from tests.workflows._weekly_rules import weekly_artifacts, weekly_triggers


@dataclass(frozen=True)
class WorkflowRule:
    name: str
    rationale: str
    check: Callable[[Workflow], Iterator[Finding]]


def declares_permissions(workflow: Workflow) -> Iterator[Finding]:
    if not workflow.permissions:
        yield Finding(
            "declares-permissions",
            workflow.stem,
            "",
            1,
            f"{workflow.path}: no workflow permissions",
        )


def has_trigger(workflow: Workflow) -> Iterator[Finding]:
    if not workflow.on_:
        yield Finding("has-trigger", workflow.stem, "", 1, f"{workflow.path}: no trigger declared")


WORKFLOW_RULES = (
    WorkflowRule("declares-permissions", "a missing block means write-all", declares_permissions),
    WorkflowRule("has-trigger", "a workflow with no trigger is dead surface", has_trigger),
    WorkflowRule(
        RuleId.CODEQL_TRIGGERS.value,
        "codeql triggers on PR main, push main, and weekly cron",
        codeql_triggers,
    ),
    WorkflowRule(
        RuleId.NIGHTLY_TRIGGERS.value,
        "nightly triggers on daily schedule and workflow_dispatch",
        nightly_triggers,
    ),
    WorkflowRule(
        RuleId.NIGHTLY_NAME.value, "nightly workflow name identifies the nightly plane", nightly_name
    ),
    WorkflowRule(
        RuleId.NIGHTLY_CONCURRENCY.value,
        "nightly declares concurrency with cancel-in-progress",
        nightly_concurrency,
    ),
    WorkflowRule(
        RuleId.WEEKLY_TRIGGERS.value,
        "weekly triggers on weekly schedule and workflow_dispatch",
        weekly_triggers,
    ),
    WorkflowRule(
        RuleId.NIGHTLY_ARTIFACTS.value,
        "nightly uploads coverage and junit artifacts across jobs",
        nightly_artifacts,
    ),
    WorkflowRule(
        RuleId.WEEKLY_ARTIFACTS.value,
        "weekly uploads mutation and resolution artifacts across jobs",
        weekly_artifacts,
    ),
    WorkflowRule(
        RuleId.CI_PACKAGING_READ_ONLY.value,
        "packaging workflow declares only contents: read and publishes nothing",
        ci_packaging_read_only,
    ),
    WorkflowRule(
        RuleId.CI_SECURITY_PERMISSIONS.value,
        "ci.yml declares exactly contents:read and pull-requests:read",
        ci_security_permissions,
    ),
    WorkflowRule(
        RuleId.CI_RELEASE_GATED_ON_CI.value,
        "every release job but the roster is downstream of the ci gate",
        ci_release_gated_on_ci,
    ),
)
