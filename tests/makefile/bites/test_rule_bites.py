"""Every rule is proved to fail on an injected violation."""

from __future__ import annotations

import pytest

from tests._ci_contract import CONFIG_PATH, MAKEFILE_PATH, WORKFLOW_PATH
from tests.makefile._model import Makefile, RecipeLine, parse_makefile
from tests.makefile._policy import RuleId
from tests.makefile._rules import (
    TARGET_RULES,
    ci_required_aggregate,
    hook_ids_resolve,
    jscpd_single_hook,
    no_gate_restated,
)
from tests.workflows._model import Job, Step, Workflow, parse_workflow


def _mutate_target(makefile: Makefile, name: str, recipe: tuple[RecipeLine, ...]) -> Makefile:
    targets = tuple(
        target.model_copy(update={"recipe": recipe}) if target.name == name else target
        for target in makefile.targets
    )
    return makefile.model_copy(update={"targets": targets})


def _mutate_step_waived(job: Job, step_name: str, **changes) -> Job:
    """Rebuild one step with the given changes, admitting truthy continue-on-error via waiver."""
    step = next(s for s in job.steps if s.name == step_name)
    raw = step.model_dump(by_alias=True)
    coerced = dict(changes)
    if "continue_on_error" in coerced:
        raw["continue-on-error"] = coerced.pop("continue_on_error")
    raw.update(coerced)
    context = {"waived_lines": {step.line}} if raw.get("continue-on-error") is True else None
    new_step = Step.create(raw, context=context)
    steps = tuple(new_step if s.name == step_name else s for s in job.steps)
    return job.model_copy(update={"steps": steps})


def _set_step_field(job: Job, step_name: str, field: str, value: object) -> Job:
    """Set one field on a step via a dict update (no ``shell=`` keyword, which ruff S604 flags)."""
    step = next(s for s in job.steps if s.name == step_name)
    new_step = step.model_copy(update={field: value})
    steps = tuple(new_step if s.name == step_name else s for s in job.steps)
    return job.model_copy(update={"steps": steps})


def _broken_workflow(broken_job: Job) -> Workflow:
    base = parse_workflow(WORKFLOW_PATH)
    jobs = dict(base.jobs)
    jobs["required"] = broken_job
    return base.model_copy(update={"jobs": jobs})


MAKEFILE = parse_makefile(MAKEFILE_PATH)
LINT = next(target for target in MAKEFILE.targets if target.name == "lint")


@pytest.mark.parametrize(
    ("rule_fn", "rule_id", "broken_target"),
    [
        (
            hook_ids_resolve,
            RuleId.MAKEFILE_HOOK_IDS_RESOLVE,
            _mutate_target(
                MAKEFILE,
                "lint",
                (RecipeLine(command="$(PRE_COMMIT) run mypy-renamed", line=LINT.line),),
            ),
        ),
        (
            no_gate_restated,
            RuleId.MAKEFILE_NO_GATE_RESTATED,
            _mutate_target(
                MAKEFILE,
                "lint",
                (
                    RecipeLine(
                        command='cd "$(SUITE_DIR)" && $(PYTHON) -m ruff check app tests scripts',
                        line=LINT.line,
                    ),
                ),
            ),
        ),
    ],
)
def test_a_makefile_rule_names_the_injected_violation(
    rule_fn, rule_id: RuleId, broken_target: Makefile
) -> None:
    target = next(t for t in broken_target.targets if t.name == LINT.name)
    findings = list(rule_fn(MAKEFILE, target))
    assert findings, f"rule {rule_id} did not flag the injected violation"
    assert findings[0].rule == rule_id
    assert findings[0].target == LINT.name
    assert MAKEFILE_PATH.name in findings[0].message


def _required_job() -> Job:
    return parse_workflow(WORKFLOW_PATH).jobs["required"]


@pytest.mark.parametrize(
    ("mutation", "expected_hint"),
    [
        (lambda job: job.model_copy(update={"if_": "${{ false }}"}), "always"),
        (
            lambda job: job.model_copy(update={"needs": tuple(n for n in job.needs if str(n) != "battery")}),
            "battery",
        ),
        (
            lambda job: _mutate_step_waived(
                job, "Require every gate to have succeeded", continue_on_error=True
            ),
            "continue-on-error",
        ),
        (
            lambda job: _set_step_field(job, "Require every gate to have succeeded", "shell", "sh"),
            "shell",
        ),
    ],
)
def test_ci_required_aggregate_names_the_injected_violation(mutation, expected_hint) -> None:
    broken = _broken_workflow(mutation(_required_job()))
    findings = list(ci_required_aggregate(broken, broken.jobs["required"]))
    assert findings, f"ci-required-aggregate did not flag the {expected_hint} evasion"
    assert findings[0].rule == RuleId.CI_REQUIRED_AGGREGATE


def test_ci_required_aggregate_refuses_a_deleted_if() -> None:
    broken = _broken_workflow(_required_job().model_copy(update={"if_": None}))
    findings = list(ci_required_aggregate(broken, broken.jobs["required"]))
    assert findings, "ci-required-aggregate did not flag the deleted if:"
    assert findings[0].rule == RuleId.CI_REQUIRED_AGGREGATE
    assert findings[0].target == "required"
    assert f"{WORKFLOW_PATH.name}:" in findings[0].message


def test_dropped_result_assertion_is_caught() -> None:
    base = _required_job()
    step = next(s for s in base.steps if str(s.run or "").strip())
    script = str(step.run or "")
    assertion = 'test "${{ needs.battery.result }}" = "success"\n'
    assert assertion in script, "the battery assertion needle must be present"
    broken_step = step.model_copy(update={"run": script.replace(assertion, "", 1)})
    broken_job = _mutate_step_waived(base, "Require every gate to have succeeded", run=broken_step.run)
    broken = _broken_workflow(broken_job)
    findings = list(ci_required_aggregate(broken, broken.jobs["required"]))
    assert findings
    assert "battery" in findings[0].message


def test_script_execution_passes_when_all_needs_succeed() -> None:
    wf = parse_workflow(WORKFLOW_PATH)
    findings = list(ci_required_aggregate(wf, wf.jobs["required"]))
    assert not findings, "\n".join(f.message for f in findings)


def test_every_target_rule_has_a_bite() -> None:
    assert {rule.name for rule in TARGET_RULES} == {
        RuleId.MAKEFILE_HOOK_IDS_RESOLVE,
        RuleId.MAKEFILE_NO_GATE_RESTATED,
    }


def test_jscpd_single_hook_passes_on_the_live_config() -> None:
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    assert not list(jscpd_single_hook(config_text))


def test_jscpd_single_hook_rejects_a_second_quoted_jscpd_hook() -> None:
    from tests._ci_contract import QUOTED_SECOND_JSCPD_HOOK

    findings = list(jscpd_single_hook(QUOTED_SECOND_JSCPD_HOOK))
    assert findings
    assert findings[0].rule == RuleId.JSCPD_SINGLE_HOOK
    assert "jscpd" in findings[0].message


def test_every_ci_rule_has_a_bite() -> None:
    assert ci_required_aggregate.__name__.replace("_", "-") == "ci-required-aggregate"
