"""Typed rules for the ci.yml history/requirements contract."""

from __future__ import annotations

from collections.abc import Iterator

from tests.workflows._model import Job, Step, Workflow
from tests.workflows._policy import Finding, RuleId


def _checkout_steps(job: Job) -> list[Step]:
    return [step for step in job.steps if step.uses and step.uses.startswith("actions/checkout@")]


def _fetch_depth(step: Step) -> str | None:
    return str(step.with_.get("fetch-depth", ""))


def ci_secret_scan_fetch_depth(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "secret-scan":
        return
    checkouts = _checkout_steps(job)
    if len(checkouts) != 1:
        yield Finding(
            RuleId.CI_SECRET_SCAN_FETCH_DEPTH.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: secret-scan must check out once",
        )
        return
    if _fetch_depth(checkouts[0]) != "0":
        yield Finding(
            RuleId.CI_SECRET_SCAN_FETCH_DEPTH.value,
            workflow.stem,
            job.key,
            checkouts[0].line,
            f"{workflow.path}:{checkouts[0].line}: secret-scan checkout must use fetch-depth 0",
        )


def ci_dependency_review_checkout(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "dependency-review":
        return
    checkouts = _checkout_steps(job)
    if len(checkouts) != 1:
        yield Finding(
            RuleId.CI_DEPENDENCY_REVIEW_CHECKOUT.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: dependency-review must check out once",
        )


def ci_dependency_review_refs(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "dependency-review":
        return
    for step in job.steps:
        uses = step.uses or ""
        if "dependency-review-action@" in uses:
            base_ref = str(step.with_.get("base-ref", ""))
            head_ref = str(step.with_.get("head-ref", ""))
            if "github.event.before" not in base_ref:
                yield Finding(
                    RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: dependency-review base-ref must cover github.event.before",
                )
            if "github.sha" not in head_ref:
                yield Finding(
                    RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: dependency-review head-ref must cover github.sha",
                )
            return
    yield Finding(
        RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
        workflow.stem,
        job.key,
        job.line,
        f"{workflow.path}:{job.line}: dependency-review step not found",
    )


def ci_real_pbkdf2_door(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci":
        return
    if job.key == "real-pbkdf2":
        if job.if_ is not None:
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: real-pbkdf2 job must not carry a job-level if",
            )
        door_steps = [step for step in job.steps if step.uses and "run-tests" in step.uses]
        if len(door_steps) != 1:
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: real-pbkdf2 job must call the run-tests door exactly once",
            )
            return
        door = door_steps[0]
        if door.if_ is not None:
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                door.line,
                f"{workflow.path}:{door.line}: real-pbkdf2 door step must not carry a step-level if",
            )
        if str(door.with_.get("marker", "")) != "real_pbkdf2":
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                door.line,
                f"{workflow.path}:{door.line}: real-pbkdf2 door must pass marker real_pbkdf2",
            )
    if job.key == "coverage":
        for step in job.steps:
            name = str(step.name or "")
            if name.startswith("Download real-pbkdf2") or name.startswith("Upload real-pbkdf2"):
                guard = str(step.if_ or "")
                if "needs.changes.outputs.real-pbkdf2 == 'true'" not in guard:
                    yield Finding(
                        RuleId.CI_REAL_PBKDF2_DOOR.value,
                        workflow.stem,
                        job.key,
                        step.line,
                        f"{workflow.path}:{step.line}: coverage step {name!r} lost the real-pbkdf2 guard",
                    )
