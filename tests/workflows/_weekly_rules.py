"""Typed rules for the weekly workflow contract."""

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


def weekly_triggers(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "weekly":
        return
    on = workflow.on_
    if _schedule_crons(on) != ["0 4 * * 0"]:
        yield Finding(
            RuleId.WEEKLY_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: weekly schedule must be 0 4 * * 0",
        )
    if "workflow_dispatch" not in on:
        yield Finding(
            RuleId.WEEKLY_TRIGGERS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: weekly requires workflow_dispatch",
        )


def weekly_mutation_timebox(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "weekly" or job.key != "mutation-testing":
        return
    if job.timeout_minutes is None or job.timeout_minutes > 120:
        yield Finding(
            RuleId.WEEKLY_MUTATION_TIMEBOX.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: mutation-testing timeout must be <= 120 minutes",
        )


def weekly_mutation_tool(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "weekly" or job.key != "mutation-testing":
        return
    if not any(step.run and "mutmut" in step.run for step in job.steps):
        yield Finding(
            RuleId.WEEKLY_MUTATION_TOOL.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: mutation-testing must run mutmut",
        )


def weekly_mutation_targets(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "weekly" or job.key != "mutation-testing":
        return
    for step in job.steps:
        run = step.run or ""
        if "mutmut run" in run:
            if not any(token in run for token in ("invariants", "admission", "core")):
                yield Finding(
                    RuleId.WEEKLY_MUTATION_TARGETS.value,
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: mutmut must target invariant-critical modules",
                )
            return
    yield Finding(
        RuleId.WEEKLY_MUTATION_TARGETS.value,
        workflow.stem,
        job.key,
        job.line,
        f"{workflow.path}:{job.line}: mutmut run step targeting invariant-critical modules not found",
    )


def weekly_lowest_direct(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "weekly" or job.key != "lowest-direct-resolution":
        return
    has_compile = False
    has_clean_venv = False
    has_import_check = False
    for step in job.steps:
        run = step.run or ""
        name = str(step.name or "").lower()
        if "uv pip compile" in run and "--resolution lowest-direct" in run:
            has_compile = True
        if "venv" in run and any(token in name for token in ("clean", "fresh", "new")):
            has_clean_venv = True
        if "import" in run and any(token in run for token in ("crawlutron", "admission", "safe_url")):
            has_import_check = True
    if not has_compile:
        yield Finding(
            RuleId.WEEKLY_LOWEST_DIRECT.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: uv pip compile --resolution lowest-direct step required",
        )
    if not has_clean_venv:
        yield Finding(
            RuleId.WEEKLY_LOWEST_DIRECT.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: clean venv creation step required",
        )
    if not has_import_check:
        yield Finding(
            RuleId.WEEKLY_LOWEST_DIRECT.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: import every member verification step required",
        )


def weekly_artifacts(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "weekly":
        return
    has_mutation = False
    has_resolution = False
    for job in workflow.jobs.values():
        for step in job.steps:
            uses = step.uses or ""
            if "upload-artifact" in uses:
                path = str(step.with_.get("path", ""))
                name = str(step.name or "").lower()
                if "mutation" in name or "html" in path:
                    has_mutation = True
                if "resolution" in name or "lowest" in name or "probe" in name:
                    has_resolution = True
    if not has_mutation:
        yield Finding(
            RuleId.WEEKLY_ARTIFACTS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: weekly must upload a mutation testing artifact",
        )
    if not has_resolution:
        yield Finding(
            RuleId.WEEKLY_ARTIFACTS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: weekly must upload a resolution probe artifact",
        )
