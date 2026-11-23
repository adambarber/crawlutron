"""Total rules over the typed workflow model; waivers are named and ratcheted."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from tests.workflows._ci_rules import (
    ci_dependency_review_checkout,
    ci_dependency_review_refs,
    ci_real_pbkdf2_door,
    ci_secret_scan_fetch_depth,
)
from tests.workflows._codeql_rules import (
    codeql_no_build_step,
    codeql_registry_pin,
    codeql_security_events_write,
    codeql_security_extended,
    codeql_single_sarif_upload,
    codeql_triggers,
)
from tests.workflows._model import POLICY, WORKFLOWS, Job, Workflow
from tests.workflows._nightly_rules import (
    nightly_artifacts,
    nightly_concurrency,
    nightly_coverage_reports,
    nightly_door_call,
    nightly_name,
    nightly_triggers,
)
from tests.workflows._policy import Finding, RuleId, Scope
from tests.workflows._weekly_rules import (
    weekly_artifacts,
    weekly_lowest_direct,
    weekly_mutation_targets,
    weekly_mutation_timebox,
    weekly_mutation_tool,
    weekly_triggers,
)


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
        if POLICY.is_waived(workflow.stem, job.key, step.name, RuleId.NO_BARE_PYTEST):
            continue
        for invocation in step.command_facts.pytest_invocations:
            if invocation.filtered:
                continue
            if not _invocation_is_direct_pytest(invocation.text):
                continue
            yield Finding(
                "no-bare-pytest",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: a second default-suite run can drift from the battery: "
                f"{invocation.text}",
            )


def _invocation_is_direct_pytest(text: str) -> bool:
    """True when pytest is the command being run, not an argument to another tool."""
    stripped = text.strip()
    if stripped.startswith("xvfb-run -a "):
        stripped = stripped[len("xvfb-run -a ") :]
    if stripped.startswith("pytest") and (len(stripped) == 6 or stripped[6] in " \t"):
        return True
    if stripped.startswith("python -m pytest") or stripped.startswith("python3 -m pytest"):
        return True
    for prefix in (".venv/bin/python ", ".venv/bin/python3 "):
        if stripped.startswith(prefix):
            rest = stripped[len(prefix) :]
            if rest.startswith("-m pytest") and (len(rest) == 9 or rest[9] in " \t"):
                return True
    return False


def no_pip_install(workflow: Workflow, job: Job) -> Iterator[Finding]:
    for step in job.steps:
        if step.command_facts.pip_install and not POLICY.is_waived(
            workflow.stem, job.key, step.name, RuleId.NO_PIP_INSTALL
        ):
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


def uses_tests_door(workflow: Workflow, job: Job) -> Iterator[Finding]:
    """Every job with a direct pytest invocation must call the shared run-tests door."""
    if not _job_directly_runs_pytest(job):
        return
    calls_door = any(step.uses and "run-tests" in str(step.uses) for step in job.steps)
    if not calls_door:
        yield Finding(
            "tests-use-door",
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: job runs pytest directly but does not call "
            f"the shared run-tests door",
        )


def no_silent_uploads(workflow: Workflow, job: Job) -> Iterator[Finding]:
    """No step may swallow evidence: fail_ci_if_error:false, if-no-files-found:warn,
    or an artifact consumer gated on a re-derived path filter instead of the producer result."""
    for step in job.steps:
        if step.with_.get("fail_ci_if_error") is False:
            yield Finding(
                "no-silent-uploads",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: {step.name or 'unnamed step'} sets fail_ci_if_error:false",
            )
        if step.with_.get("if-no-files-found") == "warn":
            yield Finding(
                "no-silent-uploads",
                workflow.stem,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: {step.name or 'unnamed step'} sets if-no-files-found:warn",
            )
        if step.uses and "download-artifact" in str(step.uses):
            guard = str(step.if_) if step.if_ is not None else ""
            if "changes.outputs.browser" in guard:
                yield Finding(
                    "no-silent-uploads",
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: {step.name or 'unnamed step'} gates a browser download on "
                    f"changes.outputs.browser instead of needs.test-browser.result",
                )


def _job_directly_runs_pytest(job: Job) -> bool:
    for step in job.steps:
        for invocation in step.command_facts.pytest_invocations:
            if _invocation_is_direct_pytest(invocation.text):
                return True
    return False


def door_no_if(workflow: Workflow, job: Job) -> Iterator[Finding]:
    """The door step may not carry a step-level `if:` -- a skipped door must be
    unrepresentable so the refusal inside the action can never be bypassed."""
    for step in job.steps:
        if step.uses and "run-tests" in str(step.uses):
            if step.if_ is not None:
                yield Finding(
                    "door-no-if",
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: the run-tests door step carries "
                    f"step-level if={step.if_!r}, which could skip the refusal",
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
    Rule("uses-tests-door", Scope.WITH_STEPS, "every pytest job calls the shared door", uses_tests_door),
    Rule(
        "door-no-if",
        Scope.WITH_STEPS,
        "the door step may not carry a step-level if",
        door_no_if,
    ),
    Rule("no-pip-install", Scope.WITH_STEPS, "the lock freezes the graph", no_pip_install),
    Rule("no-heredoc", Scope.WITH_STEPS, "a heredoc hides control flow", no_heredoc),
    Rule("unique-step-ids", Scope.WITH_STEPS, "step outputs are addressed by id", unique_step_ids),
    Rule(
        "no-silent-uploads",
        Scope.WITH_STEPS,
        "no silent uploads or unguarded artifact consumers",
        no_silent_uploads,
    ),
    Rule(
        RuleId.CODEQL_SECURITY_EVENTS_WRITE.value,
        Scope.ALL,
        "codeql analyze job holds security-events:write",
        codeql_security_events_write,
    ),
    Rule(
        RuleId.CODEQL_REGISTRY_PIN.value,
        Scope.WITH_STEPS,
        "codeql init/analyze use the registry-pinned SHA",
        codeql_registry_pin,
    ),
    Rule(
        RuleId.CODEQL_SECURITY_EXTENDED.value,
        Scope.WITH_STEPS,
        "codeql init uses security-extended queries",
        codeql_security_extended,
    ),
    Rule(
        RuleId.CODEQL_NO_BUILD_STEP.value,
        Scope.WITH_STEPS,
        "codeql python job has no build step",
        codeql_no_build_step,
    ),
    Rule(
        RuleId.CODEQL_SINGLE_SARIF_UPLOAD.value,
        Scope.WITH_STEPS,
        "codeql analyze uploads SARIF once, no redundant upload step",
        codeql_single_sarif_upload,
    ),
    Rule(
        RuleId.NIGHTLY_DOOR_CALL.value,
        Scope.WITH_STEPS,
        "nightly test job calls the run-tests door",
        nightly_door_call,
    ),
    Rule(
        RuleId.NIGHTLY_COVERAGE_REPORTS.value,
        Scope.WITH_STEPS,
        "nightly runs scripts.coverage_reports",
        nightly_coverage_reports,
    ),
    Rule(
        RuleId.WEEKLY_MUTATION_TIMEBOX.value,
        Scope.ALL,
        "weekly mutation-testing job is timeboxed",
        weekly_mutation_timebox,
    ),
    Rule(
        RuleId.WEEKLY_MUTATION_TOOL.value,
        Scope.WITH_STEPS,
        "weekly runs mutmut for mutation testing",
        weekly_mutation_tool,
    ),
    Rule(
        RuleId.WEEKLY_MUTATION_TARGETS.value,
        Scope.WITH_STEPS,
        "weekly mutmut targets invariant-critical modules",
        weekly_mutation_targets,
    ),
    Rule(
        RuleId.WEEKLY_LOWEST_DIRECT.value,
        Scope.WITH_STEPS,
        "weekly lowest-direct probe compiles and imports",
        weekly_lowest_direct,
    ),
    Rule(
        RuleId.CI_SECRET_SCAN_FETCH_DEPTH.value,
        Scope.WITH_STEPS,
        "ci secret-scan checks out with fetch-depth 0",
        ci_secret_scan_fetch_depth,
    ),
    Rule(
        RuleId.CI_DEPENDENCY_REVIEW_CHECKOUT.value,
        Scope.WITH_STEPS,
        "ci dependency-review checks out the trusted tree",
        ci_dependency_review_checkout,
    ),
    Rule(
        RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
        Scope.WITH_STEPS,
        "ci dependency-review supplies base/head refs",
        ci_dependency_review_refs,
    ),
    Rule(
        RuleId.CI_REAL_PBKDF2_DOOR.value,
        Scope.WITH_STEPS,
        "ci real-pbkdf2 job calls the run-tests door with no step-level if",
        ci_real_pbkdf2_door,
    ),
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
        RuleId.NIGHTLY_NAME.value,
        "nightly workflow name identifies the nightly plane",
        nightly_name,
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
)

CASES = tuple(
    (item, job, rule)
    for item in WORKFLOWS
    for job in item.jobs.values()
    for rule in JOB_RULES
    if rule.scope.matches(job)
)
WF_CASES = tuple((item, rule) for item in WORKFLOWS for rule in WORKFLOW_RULES)
