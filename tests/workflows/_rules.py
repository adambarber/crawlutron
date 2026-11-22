"""Total rules over the typed workflow model; waivers are named and ratcheted."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum, auto

from tests.workflows._model import WORKFLOWS, Job, Workflow

BARE_PYTEST_WAIVERS = frozenset(
    {
        ("ci", "test-browser", "Run pytest (all markers, main push and release calls)"),
        ("nightly", "test", "Run full test suite (no -m filter) with coverage"),
        ("weekly", "mutation-testing", "Run mutation testing on invariant-critical modules"),
    }
)
PIP_INSTALL_WAIVERS = frozenset(
    {("weekly", "lowest-direct-resolution", "Install lowest-direct dependencies")}
)


@dataclass(frozen=True)
class Finding:
    rule: str
    workflow: str
    job: str
    line: int
    message: str


class Scope(Enum):
    ALL = auto()
    WITH_STEPS = auto()

    def matches(self, job: Job) -> bool:
        return self is Scope.ALL or bool(job.steps)


@dataclass(frozen=True)
class Rule:
    name: str
    scope: Scope
    rationale: str
    check: Callable[[Workflow, Job], Iterator[Finding]]


@dataclass(frozen=True)
class WorkflowRule:
    name: str
    rationale: str
    check: Callable[[Workflow], Iterator[Finding]]


def needs_resolve(workflow: Workflow, job: Job) -> Iterator[Finding]:
    for need in job.needs:
        if need not in workflow.jobs:
            yield Finding(
                "needs-resolve",
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: needs {need!r} names no job",
            )


def no_event_splicing_in_run(workflow: Workflow, job: Job) -> Iterator[Finding]:
    for step in job.steps:
        if step.event_spliced:
            yield Finding(
                "no-event-splicing-in-run",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: event input reaches a script without env binding",
            )


def no_bare_pytest(workflow: Workflow, job: Job) -> Iterator[Finding]:
    for step in job.steps:
        if (workflow.stem, job.key, step.name) in BARE_PYTEST_WAIVERS:
            continue
        if step.pytest_env_marker or step.command_facts.exported_markers:
            continue
        for segment in step.command_facts.unfiltered_pytest_segments:
            yield Finding(
                "no-bare-pytest",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: a second default-suite run can drift from the battery: "
                f"{segment.strip()}",
            )


def no_pip_install(workflow: Workflow, job: Job) -> Iterator[Finding]:
    for step in job.steps:
        if step.command_facts.pip_install and (workflow.stem, job.key, step.name) not in PIP_INSTALL_WAIVERS:
            yield Finding(
                "no-pip-install",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: pip re-resolves the frozen graph",
            )


def no_heredoc(workflow: Workflow, job: Job) -> Iterator[Finding]:
    for step in job.steps:
        if step.command_facts.heredoc:
            yield Finding(
                "no-heredoc",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: a heredoc hides shell control flow",
            )


def unique_step_ids(workflow: Workflow, job: Job) -> Iterator[Finding]:
    ids = [step.id for step in job.steps if step.id]
    if len(ids) != len(set(ids)):
        yield Finding(
            "unique-step-ids",
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: duplicate step id in {job.key}",
        )


JOB_RULES = (
    Rule("needs-resolve", Scope.ALL, "a needs edge to a missing job never becomes green", needs_resolve),
    Rule(
        "no-event-splicing-in-run",
        Scope.WITH_STEPS,
        "event text must reach a script only through env",
        no_event_splicing_in_run,
    ),
    Rule("no-bare-pytest", Scope.WITH_STEPS, "one default suite per push", no_bare_pytest),
    Rule("no-pip-install", Scope.WITH_STEPS, "the lock freezes the graph", no_pip_install),
    Rule("no-heredoc", Scope.WITH_STEPS, "a heredoc hides control flow", no_heredoc),
    Rule("unique-step-ids", Scope.WITH_STEPS, "step outputs are addressed by id", unique_step_ids),
)


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
)

CASES = tuple(
    (item, job, rule)
    for item in WORKFLOWS
    for job in item.jobs.values()
    for rule in JOB_RULES
    if rule.scope.matches(job)
)
WF_CASES = tuple((item, rule) for item in WORKFLOWS for rule in WORKFLOW_RULES)
