"""Typed rules for the nightly workflow contract."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from tests.workflows._model import Job, Workflow
from tests.workflows._policy import Finding, RuleId


def _schedule_crons(on: Mapping[str, object]) -> list[str]:
    schedule = on.get("schedule")
    if isinstance(schedule, list):
        crons: list[str] = []
        for entry in schedule:
            if isinstance(entry, dict) and isinstance(entry.get("cron"), str):
                crons.append(entry["cron"])
        return crons
    return []


def nightly_triggers(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "nightly":
        return
    on = workflow.on_
    if _schedule_crons(on) != ["0 3 * * *"]:
        yield Finding(
            RuleId.NIGHTLY_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: nightly schedule must be 0 3 * * *",
        )
    if "workflow_dispatch" not in on:
        yield Finding(
            RuleId.NIGHTLY_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: nightly requires workflow_dispatch",
        )


def nightly_name(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "nightly":
        return
    if "nightly" not in (workflow.name or "").lower():
        yield Finding(
            RuleId.NIGHTLY_NAME.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: workflow name must contain 'nightly'",
        )


def nightly_concurrency(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "nightly":
        return
    if workflow.concurrency is None:
        yield Finding(
            RuleId.NIGHTLY_CONCURRENCY.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: nightly must declare concurrency",
        )


def nightly_door_call(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "nightly" or job.key != "test":
        return
    if not any(step.uses and "run-tests" in step.uses for step in job.steps):
        yield Finding(
            RuleId.NIGHTLY_DOOR_CALL.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: nightly test job must call the run-tests door",
        )


def nightly_coverage_reports(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "nightly":
        return
    reports = any(step.run and "scripts.coverage_reports" in step.run for step in job.steps)
    if job.key == "coverage" and not reports:
        yield Finding(
            RuleId.NIGHTLY_COVERAGE_REPORTS.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: nightly consumer must report after combining fragments",
        )
    if job.key == "test" and reports:
        yield Finding(
            RuleId.NIGHTLY_COVERAGE_REPORTS.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: producer cannot report from the door's uncombined fragment",
        )


def nightly_artifacts(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "nightly":
        return
    has_coverage = False
    has_junit = False
    for job in workflow.jobs.values():
        for step in job.steps:
            uses = step.uses or ""
            if "upload-artifact" in uses:
                path = str(step.with_.get("path", ""))
                name = str(step.name or "").lower()
                if "coverage" in name or "coverage" in path:
                    has_coverage = True
                if "junit" in name or "junit" in path or "xml" in path:
                    has_junit = True
    if not has_coverage:
        yield Finding(
            RuleId.NIGHTLY_ARTIFACTS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: nightly must upload a coverage artifact",
        )
    if not has_junit:
        yield Finding(
            RuleId.NIGHTLY_ARTIFACTS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: nightly must upload a junit artifact",
        )
