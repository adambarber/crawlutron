"""Total rules over the typed Makefile model and the typed ci.yml model."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum, auto

from tests._ci_contract import (
    CONFIG_PATH,
    ERREXIT_SHELL,
    REPORTING_JOBS,
    _hook_blocks,
    _render_results,
    _result_assertion,
    _run_script,
)
from tests.makefile._model import Makefile, Target
from tests.makefile._policy import RuleId
from tests.workflows._model import Job, Workflow

# How a Makefile recipe names a hook: the resolved binary or the Makefile's own indirection.
MAKEFILE_HOOK_ID = re.compile(r"(?:\$\(PRE_COMMIT\)|\bpre-commit)\s+run\s+(\S+)")

# How a gate tool may be spelled in a Makefile recipe, as a pattern rather than a literal string.
GATE_TOOL_PATTERNS = (
    r"-m\s+ruff\b",
    r"-m\s+mypy\b",
    r"scripts/jscpd_gate\b",
    r"basedpyright\b",
    r"tests/invariants/\b",
)

_CONFIG_HOOKS = tuple(_hook_blocks(CONFIG_PATH.read_text(encoding="utf-8")))
_KNOWN_HOOK_IDS = frozenset(hook_id for hook_id, _ in _CONFIG_HOOKS)


@dataclass(frozen=True)
class Finding:
    rule: str
    target: str
    line: int
    message: str


class Scope(Enum):
    ALL = auto()
    WITH_RECIPE = auto()

    def matches(self, target: Target) -> bool:
        return self is Scope.ALL or bool(target.recipe)


@dataclass(frozen=True)
class Rule:
    name: str
    scope: Scope
    rationale: str
    check: Callable[[Makefile, Target], Iterator[Finding]]


def hook_ids_resolve(makefile: Makefile, target: Target) -> Iterator[Finding]:
    """Every hook id the Makefile delegates to must be declared in .pre-commit-config.yaml."""
    referenced: set[str] = set()
    for line in target.recipe:
        referenced |= set(MAKEFILE_HOOK_ID.findall(line.command))
    unknown = sorted(referenced - _KNOWN_HOOK_IDS)
    if unknown:
        yield Finding(
            RuleId.MAKEFILE_HOOK_IDS_RESOLVE,
            target.name,
            target.line,
            f"{makefile.path}:{target.line}: target {target.name!r} names hook ids the config "
            f"does not declare: {unknown}; declared ids are {sorted(_KNOWN_HOOK_IDS)}",
        )


def no_gate_restated(makefile: Makefile, target: Target) -> Iterator[Finding]:
    """No recipe runs a gate tool directly instead of by hook id."""
    for line in target.recipe:
        for pattern in GATE_TOOL_PATTERNS:
            if re.search(pattern, line.command):
                yield Finding(
                    RuleId.MAKEFILE_NO_GATE_RESTATED,
                    target.name,
                    line.line,
                    f"{makefile.path}:{line.line}: target {target.name!r} runs a gate tool directly "
                    f"({line.command!r}): run it as `pre-commit run <hook-id>` so the command stays "
                    "in .pre-commit-config.yaml, which is the only declaration of it",
                )


def jscpd_single_hook(config_text: str) -> Iterator[Finding]:
    """Exactly one jscpd gate is declared in .pre-commit-config.yaml, quoted ids included."""
    hooks = _hook_blocks(config_text)
    jscpd = [hook_id for hook_id, _ in hooks if hook_id.split("-")[0] == "jscpd"]
    if len(jscpd) != 1 or jscpd[0] != "jscpd":
        yield Finding(
            RuleId.JSCPD_SINGLE_HOOK,
            "makefile",
            1,
            f".pre-commit-config.yaml must declare exactly one jscpd gate (the whole-service "
            f"one), found {jscpd}",
        )


TARGET_RULES = (
    Rule(
        RuleId.MAKEFILE_HOOK_IDS_RESOLVE,
        Scope.WITH_RECIPE,
        "a renamed hook leaves `make lint` exiting non-zero with 'hook not found'",
        hook_ids_resolve,
    ),
    Rule(
        RuleId.MAKEFILE_NO_GATE_RESTATED,
        Scope.WITH_RECIPE,
        "the Makefile is not a second declaration of every gate",
        no_gate_restated,
    ),
)


def _needs_referenced(script: str) -> set[str]:
    """Every ``needs.<job>.result`` job name asserted in the script text."""
    import re

    return set(re.findall(r"\$\{\{ needs\.([A-Za-z0-9_-]+)\.result \}\}", script))


def _finding(rule: RuleId, target: str, line: int, message: str) -> Finding:
    return Finding(rule, target, line, message)


def ci_required_aggregate(workflow: Workflow, job: Job) -> Iterator[Finding]:
    """The required aggregate must propagate every failure."""
    if job.if_ is None or str(job.if_) != "${{ always() }}":
        yield _finding(
            RuleId.CI_REQUIRED_AGGREGATE,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: the required aggregate must run even when a need fails",
        )
    needs = {str(need) for need in job.needs}
    yield from _needs_complete(job, needs, workflow)
    step = _gate_step(job)
    yield from _step_no_continue_on_error(step, job, workflow)
    yield from _step_shell_is_errexit(step, job, workflow)
    script = str(step.run or "")
    yield from _assertions_for_every_need(script, needs, step, job, workflow)
    referenced = _needs_referenced(script)
    if referenced - needs:
        yield _finding(
            RuleId.CI_REQUIRED_AGGREGATE,
            job.key,
            step.line,
            f"{workflow.path}:{step.line}: the script asserts needs for "
            f"{sorted(referenced - needs)} that are not declared in needs",
        )
        return
    yield from _script_passes_on_all_success(script, needs, step, job, workflow)
    yield from _script_fails_on_any_failure(script, needs, step, job, workflow)


def _needs_complete(job: Job, needs: set[str], workflow: Workflow) -> Iterator[Finding]:
    missing = REPORTING_JOBS - needs
    if missing:
        yield _finding(
            RuleId.CI_REQUIRED_AGGREGATE,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: the required aggregate must depend on every reporting "
            f"job, missing {sorted(missing)}",
        )


def _step_no_continue_on_error(step, job, workflow) -> Iterator[Finding]:
    if step.continue_on_error is True:
        yield _finding(
            RuleId.CI_REQUIRED_AGGREGATE,
            job.key,
            step.line,
            f"{workflow.path}:{step.line}: a step in the required aggregate is continue-on-error, "
            "so needs.result is hollow",
        )


def _step_shell_is_errexit(step, job, workflow) -> Iterator[Finding]:
    if str(step.shell or "") != ERREXIT_SHELL:
        yield _finding(
            RuleId.CI_REQUIRED_AGGREGATE,
            job.key,
            step.line,
            f"{workflow.path}:{step.line}: the required aggregate must declare shell "
            f"{ERREXIT_SHELL!r}, found {step.shell!r}: without errexit its exit code is the last "
            "test line's, not the failing one",
        )


def _assertions_for_every_need(script, needs, step, job, workflow) -> Iterator[Finding]:
    for job_name in sorted(needs):
        if _result_assertion(job_name) not in script:
            yield _finding(
                RuleId.CI_REQUIRED_AGGREGATE,
                job.key,
                step.line,
                f"{workflow.path}:{step.line}: the required aggregate never asserts "
                f"needs.{job_name}.result succeeded",
            )


def _script_passes_on_all_success(script, needs, step, job, workflow) -> Iterator[Finding]:
    results = {name: "success" for name in needs}
    if _run_script(_render_results(script, results)).returncode != 0:
        yield _finding(
            RuleId.CI_REQUIRED_AGGREGATE,
            job.key,
            step.line,
            f"{workflow.path}:{step.line}: the required aggregate's script must pass when every "
            "need reports success",
        )


def _script_fails_on_any_failure(script, needs, step, job, workflow) -> Iterator[Finding]:
    results = {name: "success" for name in needs}
    for job_name in sorted(needs):
        for outcome in ("failure", "cancelled", "skipped"):
            broken = dict(results, **{job_name: outcome})
            if _run_script(_render_results(script, broken)).returncode == 0:
                yield _finding(
                    RuleId.CI_REQUIRED_AGGREGATE,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: needs.{job_name}.result = {outcome} still "
                    "passed the required aggregate",
                )


def _gate_step(job: Job):
    for step in job.steps:
        if str(step.run or "").strip():
            return step
    raise AssertionError("the required aggregate must run a script")
