"""Total rules over the typed workflow model; waivers are named and ratcheted."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from tests.workflows._ci_rules import (
    ci_actionlint_hook,
    ci_browser_fpgen_model,
    ci_bytecode_compileall,
    ci_coverage_distinct_junit_paths,
    ci_coverage_download_artifacts,
    ci_coverage_if_always,
    ci_coverage_junit_upload,
    ci_coverage_needs,
    ci_coverage_single_artifact,
    ci_coverage_upload_hidden,
    ci_coverage_uploads_coverage_xml,
    ci_dependency_review_checkout,
    ci_dependency_review_refs,
    ci_gate_jobs_start_from_prepare,
    ci_real_pbkdf2_door,
    ci_secret_scan_fetch_depth,
)
from tests.workflows._codeql_rules import (
    codeql_no_build_step,
    codeql_registry_pin,
    codeql_security_events_write,
    codeql_security_extended,
    codeql_single_sarif_upload,
)
from tests.workflows._model import POLICY, WORKFLOWS, Job, Workflow
from tests.workflows._nightly_rules import (
    nightly_artifacts as nightly_artifacts,
)
from tests.workflows._nightly_rules import (
    nightly_coverage_reports,
    nightly_door_call,
)
from tests.workflows._policy import Finding, RuleId, Scope
from tests.workflows._weekly_rules import (
    weekly_artifacts as weekly_artifacts,
)
from tests.workflows._weekly_rules import (
    weekly_lowest_direct,
    weekly_mutation_targets,
    weekly_mutation_timebox,
    weekly_mutation_tool,
)
from tests.workflows._workflow_rules import (
    WORKFLOW_RULES,
)
from tests.workflows._workflow_rules import (
    ci_release_gated_on_ci as ci_release_gated_on_ci,
)


@dataclass(frozen=True)
class Rule:
    name: str
    scope: Scope
    rationale: str
    check: Callable[[Workflow, Job], Iterator[Finding]]


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
    """Every direct-pytest job must call the shared run-tests door."""
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
    """No step may swallow evidence or gate artifact downloads on a re-derived filter."""
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
    """The run-tests door step may not carry a step-level if."""
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
    Rule("door-no-if", Scope.WITH_STEPS, "the door step may not carry a step-level if", door_no_if),
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
    Rule(
        RuleId.CI_ACTIONLINT_HOOK.value,
        Scope.WITH_STEPS,
        "ci actionlint job runs the actionlint hook itself",
        ci_actionlint_hook,
    ),
    Rule(
        RuleId.CI_BROWSER_FPGEN_MODEL.value,
        Scope.WITH_STEPS,
        "ci test-browser job installs the pinned fpgen model via the door",
        ci_browser_fpgen_model,
    ),
    Rule(
        RuleId.CI_BYTECODE_COMPILEALL.value,
        Scope.WITH_STEPS,
        "ci battery job precompiles bytecode over derived member src roots",
        ci_bytecode_compileall,
    ),
    Rule(
        RuleId.CI_COVERAGE_NEEDS.value,
        Scope.WITH_STEPS,
        "ci coverage job needs changes, battery, real-pbkdf2, test-browser",
        ci_coverage_needs,
    ),
    Rule(
        RuleId.CI_COVERAGE_IF_ALWAYS.value,
        Scope.WITH_STEPS,
        "ci coverage job runs if: always()",
        ci_coverage_if_always,
    ),
    Rule(
        RuleId.CI_COVERAGE_DOWNLOAD_ARTIFACTS.value,
        Scope.WITH_STEPS,
        "ci coverage job downloads all four coverage artifacts tolerantly",
        ci_coverage_download_artifacts,
    ),
    Rule(
        RuleId.CI_COVERAGE_UPLOAD_HIDDEN.value,
        Scope.WITH_STEPS,
        "ci battery/real-pbkdf2/test-browser upload hidden coverage data",
        ci_coverage_upload_hidden,
    ),
    Rule(
        RuleId.CI_COVERAGE_JUNIT_UPLOAD.value,
        Scope.WITH_STEPS,
        "ci battery/real-pbkdf2/test-browser upload junit artifacts",
        ci_coverage_junit_upload,
    ),
    Rule(
        RuleId.CI_COVERAGE_DISTINCT_JUNIT_PATHS.value,
        Scope.WITH_STEPS,
        "ci coverage job downloads junit artifacts to distinct paths",
        ci_coverage_distinct_junit_paths,
    ),
    Rule(
        RuleId.CI_COVERAGE_UPLOADS_COVERAGE_XML.value,
        Scope.WITH_STEPS,
        "ci coverage job uploads coverage_reports/coverage.xml to Codecov",
        ci_coverage_uploads_coverage_xml,
    ),
    Rule(
        RuleId.CI_COVERAGE_SINGLE_ARTIFACT.value,
        Scope.WITH_STEPS,
        "ci coverage job emits exactly one artifact",
        ci_coverage_single_artifact,
    ),
    Rule(
        RuleId.CI_GATE_JOBS_START_FROM_PREPARE.value,
        Scope.WITH_STEPS,
        "ci gate jobs start from $/.github/actions/prepare",
        ci_gate_jobs_start_from_prepare,
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
