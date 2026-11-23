"""Typed rules for the codeql workflow contract."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import scripts.pin_verify as pv
from tests.workflows._model import Job, PermissionScope, Workflow
from tests.workflows._policy import Finding, RuleId


def _trigger_branches(on: Mapping[str, object], trigger: str) -> list[str] | None:
    value = on.get(trigger)
    if isinstance(value, dict):
        branches = value.get("branches")
        if isinstance(branches, list):
            return [str(branch) for branch in branches]
    return None


def _schedule_crons(on: Mapping[str, object]) -> list[str]:
    schedule = on.get("schedule")
    if isinstance(schedule, list):
        crons: list[str] = []
        for entry in schedule:
            if isinstance(entry, dict) and isinstance(entry.get("cron"), str):
                crons.append(entry["cron"])
        return crons
    return []


def _codeql_pin() -> str:
    return pv.pin_sha(pv.load_registry()["github/codeql-action"])


def codeql_triggers(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "codeql":
        return
    on = workflow.on_
    if _trigger_branches(on, "pull_request") != ["main"]:
        yield Finding(
            RuleId.CODEQL_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: codeql pull_request must target main",
        )
    if _trigger_branches(on, "push") != ["main"]:
        yield Finding(
            RuleId.CODEQL_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: codeql push must target main",
        )
    if _schedule_crons(on) != ["0 2 * * 1"]:
        yield Finding(
            RuleId.CODEQL_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: codeql schedule must be 0 2 * * 1",
        )


def codeql_security_events_write(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "codeql" or job.key != "analyze":
        return
    if job.permissions.get(PermissionScope.SECURITY_EVENTS) != "write":
        yield Finding(
            RuleId.CODEQL_SECURITY_EVENTS_WRITE.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: analyze job must have security-events:write",
        )


def codeql_registry_pin(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "codeql" or job.key != "analyze":
        return
    pin = _codeql_pin()
    found: dict[str, str] = {}
    for step in job.steps:
        uses = step.uses or ""
        for action in ("init", "analyze"):
            marker = f"github/codeql-action/{action}@"
            if marker in uses:
                found[action] = uses.split("@", 1)[1]
    if set(found) != {"init", "analyze"}:
        yield Finding(
            RuleId.CODEQL_REGISTRY_PIN.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: missing codeql steps: {sorted(found)}",
        )
        return
    for action, sha in found.items():
        if sha != pin:
            yield Finding(
                RuleId.CODEQL_REGISTRY_PIN.value,
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: codeql-action/{action} pins {sha}, registry says {pin}",
            )


def codeql_security_extended(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "codeql" or job.key != "analyze":
        return
    for step in job.steps:
        uses = step.uses or ""
        if "codeql-action/init" in uses:
            queries = step.with_.get("queries")
            if queries != "security-extended":
                yield Finding(
                    RuleId.CODEQL_SECURITY_EXTENDED.value,
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: queries must be security-extended, got {queries!r}",
                )
            return
    yield Finding(
        RuleId.CODEQL_SECURITY_EXTENDED.value,
        workflow.stem,
        job.key,
        job.line,
        f"{workflow.path}:{job.line}: codeql-action/init step with security-extended queries not found",
    )


def codeql_no_build_step(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "codeql" or job.key != "analyze":
        return
    for step in job.steps:
        run = step.run or ""
        uses = step.uses or ""
        if "build" in run.lower() or "codeql-action/autobuild" in uses:
            yield Finding(
                RuleId.CODEQL_NO_BUILD_STEP.value,
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: Python does not need a build step",
            )


def codeql_single_sarif_upload(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "codeql" or job.key != "analyze":
        return
    upload_steps = [step for step in job.steps if "codeql-action/upload-sarif" in (step.uses or "")]
    if upload_steps:
        yield Finding(
            RuleId.CODEQL_SINGLE_SARIF_UPLOAD.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: analyze already uploads SARIF; remove redundant step(s)",
        )
    if not any("codeql-action/analyze" in (step.uses or "") for step in job.steps):
        yield Finding(
            RuleId.CODEQL_SINGLE_SARIF_UPLOAD.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: codeql-action/analyze step required",
        )
