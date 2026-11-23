"""Every config rule fails on an injected violation, never vacuously."""

from __future__ import annotations

import pytest

from tests.config._ci_model import Hook
from tests.config._model import StatusDefault
from tests.config._policy import RuleId
from tests.config._rules import LIVE, RULES, Bundle
from tests.config._script_model import ScriptFact
from tests.workflows._model import Step, Workflow


def _without_hook(bundle: Bundle, hook_id: str) -> Bundle:
    hooks = tuple(hook for hook in bundle.precommit.hooks if hook.id != hook_id)
    return bundle.model_copy(update={"precommit": bundle.precommit.model_copy(update={"hooks": hooks})})


def _duplicate_jscpd(bundle: Bundle) -> Bundle:
    extra = Hook(id="jscpd-tests", stages=("pre-commit",), entry="jscpd tests", line=1)
    hooks = (*bundle.precommit.hooks, extra)
    return bundle.model_copy(update={"precommit": bundle.precommit.model_copy(update={"hooks": hooks})})


def _set_stages(bundle: Bundle, hook_id: str, stages: tuple[str, ...]) -> Bundle:
    hooks = tuple(
        hook.model_copy(update={"stages": stages}) if hook.id == hook_id else hook
        for hook in bundle.precommit.hooks
    )
    return bundle.model_copy(update={"precommit": bundle.precommit.model_copy(update={"hooks": hooks})})


def _workflow(bundle: Bundle, stem: str) -> Workflow:
    return next(item for item in bundle.workflows if item.stem == stem)


def _replace_workflow(bundle: Bundle, stem: str, changed: Workflow) -> Bundle:
    workflows = tuple(changed if item.stem == stem else item for item in bundle.workflows)
    return bundle.model_copy(update={"workflows": workflows})


def _drop_job(bundle: Bundle, stem: str, key: str) -> Bundle:
    item = _workflow(bundle, stem)
    jobs = {name: job for name, job in item.jobs.items() if name != key}
    return _replace_workflow(bundle, stem, item.model_copy(update={"jobs": jobs}))


def _drop_step_text(bundle: Bundle, stem: str, needle: str) -> Bundle:
    item = _workflow(bundle, stem)
    jobs = {}
    for name, job in item.jobs.items():
        steps = tuple(step for step in job.steps if needle not in str(step.run or ""))
        jobs[name] = job.model_copy(update={"steps": steps})
    return _replace_workflow(bundle, stem, item.model_copy(update={"jobs": jobs}))


def _add_trigger(bundle: Bundle, stem: str, trigger: str) -> Bundle:
    item = _workflow(bundle, stem)
    return _replace_workflow(bundle, stem, item.model_copy(update={"on_": {**item.on_, trigger: {}}}))


def _set_fail_fast(bundle: Bundle) -> Bundle:
    release = _workflow(bundle, "release")
    jobs = {
        key: (
            job.model_copy(update={"strategy": job.strategy.model_copy(update={"fail_fast": True})})
            if job.strategy and job.strategy.matrix
            else job
        )
        for key, job in release.jobs.items()
    }
    return _replace_workflow(bundle, "release", release.model_copy(update={"jobs": jobs}))


def _drop_validation_gate(bundle: Bundle) -> Bundle:
    release = _workflow(bundle, "release")
    validator = next(
        job
        for job in release.jobs.values()
        if any(step.run and "scripts.release.validate_all" in step.run for step in job.steps)
    )
    publisher = next(
        job
        for job in release.jobs.values()
        if any(step.uses and "gh-action-pypi-publish" in step.uses for step in job.steps)
    )
    needs = tuple(need for need in publisher.needs if need != validator.key)
    jobs = dict(release.jobs)
    jobs[publisher.key] = publisher.model_copy(update={"needs": needs})
    return _replace_workflow(bundle, "release", release.model_copy(update={"jobs": jobs}))


def _mutate_job_step(bundle: Bundle, stem: str, key: str, needle: str, **changes) -> Bundle:
    item = _workflow(bundle, stem)
    job = item.jobs[key]
    steps = tuple(
        step.model_copy(update=changes) if needle in str(step.name or "") else step for step in job.steps
    )
    jobs = dict(item.jobs)
    jobs[key] = job.model_copy(update={"steps": steps})
    return _replace_workflow(bundle, stem, item.model_copy(update={"jobs": jobs}))


def _drop_run_step(bundle: Bundle, stem: str, key: str, needle: str) -> Bundle:
    item = _workflow(bundle, stem)
    job = item.jobs[key]
    steps = tuple(step for step in job.steps if needle not in str(step.run or ""))
    jobs = dict(item.jobs)
    jobs[key] = job.model_copy(update={"steps": steps})
    return _replace_workflow(bundle, stem, item.model_copy(update={"jobs": jobs}))


def _add_direct_codecov(bundle: Bundle) -> Bundle:
    item = _workflow(bundle, "ci")
    job = item.jobs["real-pbkdf2"]
    stray = Step.create({"uses": "codecov/codecov-action@" + "a" * 40, "name": "stray", "line": 1})
    jobs = dict(item.jobs)
    jobs["real-pbkdf2"] = job.model_copy(update={"steps": (*job.steps, stray)})
    return _replace_workflow(bundle, "ci", item.model_copy(update={"jobs": jobs}))


def _drop_string(bundle: Bundle, value: str, field: str) -> Bundle:
    facts = getattr(bundle, field)
    kept = tuple(fact for fact in facts.facts if not (fact.kind == "string" and fact.value == value))
    return bundle.model_copy(update={field: facts.model_copy(update={"facts": kept})})


def _with_run(bundle: Bundle, **changes) -> Bundle:
    run = bundle.pyproject.coverage_run.model_copy(update=changes)
    return bundle.model_copy(update={"pyproject": bundle.pyproject.model_copy(update={"coverage_run": run})})


def _downgrade_python(bundle: Bundle) -> Bundle:
    steps = tuple(
        step.model_copy(update={"inputs": {"python-version": "3.12"}})
        if step.inputs.get("python-version")
        else step
        for step in bundle.prepare.steps
    )
    return bundle.model_copy(update={"prepare": bundle.prepare.model_copy(update={"steps": steps})})


def _with_ruff(bundle: Bundle, **changes) -> Bundle:
    ruff = bundle.pyproject.ruff.model_copy(update=changes)
    return bundle.model_copy(update={"pyproject": bundle.pyproject.model_copy(update={"ruff": ruff})})


MUTATIONS = (
    (
        RuleId.CODECOV_COMPONENTS,
        lambda b: b.model_copy(
            update={"codecov": b.codecov.model_copy(update={"individual_components_is_list": False})}
        ),
        "individual_components must be a list",
    ),
    (
        RuleId.CODECOV_INFORMATIONAL,
        lambda b: b.model_copy(
            update={"codecov": b.codecov.model_copy(update={"project": StatusDefault(informational=False)})}
        ),
        "informational",
    ),
    (
        RuleId.COVERAGE_SOURCE_PKGS,
        lambda b: _with_run(b, source_pkgs=frozenset({"crawlutron"})),
        "source_pkgs",
    ),
    (
        RuleId.COVERAGE_MEASURE_ONCE,
        lambda b: _with_run(b, relative_files=False, parallel=False),
        "relative_files",
    ),
    (
        RuleId.RUFF_KNOWN_FIRST_PARTY,
        lambda b: _with_ruff(b, known_first_party=frozenset()),
        "known-first-party",
    ),
    (RuleId.RUFF_KNOWN_FIRST_PARTY, lambda b: _with_ruff(b, ignored=frozenset({"D209"})), "D209"),
    (RuleId.PRECOMMIT_HOOK_SURFACE, lambda b: _without_hook(b, "ruff"), "ruff"),
    (RuleId.JSCPD_SINGLE_GATE, _duplicate_jscpd, "jscpd-tests"),
    (RuleId.COVERAGE_AT_PREPUSH, lambda b: _set_stages(b, "coverage", ("pre-commit",)), "pre-push"),
    (RuleId.PREPARE_ACTION_ENV, _downgrade_python, "3.14"),
    (
        RuleId.CONFTEST_BYTECODE_ENABLED,
        lambda b: b.model_copy(
            update={
                "conftest": b.conftest.model_copy(
                    update={
                        "facts": (
                            *b.conftest.facts,
                            ScriptFact(kind="env-key", name="PYTHONDONTWRITEBYTECODE", line=1),
                        )
                    }
                )
            }
        ),
        "PYTHONDONTWRITEBYTECODE",
    ),
    (
        RuleId.IMAGE_RELEASE_SCRIPT,
        lambda b: _drop_string(b, "CRAWLUTRON_HOST=0.0.0.0", "image"),
        "CRAWLUTRON_HOST",
    ),
    (RuleId.CI_SUPPLY_CHAIN_JOBS, lambda b: _drop_job(b, "ci", "audit"), "audit"),
    (
        RuleId.CI_ACTIONLINT_IN_REPORTING,
        lambda b: b.model_copy(update={"reporting_jobs": frozenset()}),
        "actionlint",
    ),
    (RuleId.PACKAGING_TRIGGERS, lambda b: _add_trigger(b, "packaging", "push"), "push"),
    (
        RuleId.PACKAGING_VERIFY_STEPS,
        lambda b: _drop_step_text(b, "packaging", "scripts.check_packaging"),
        "check_packaging",
    ),
    (
        RuleId.CI_JUNIT_UPLOAD_SHAPE,
        lambda b: _mutate_job_step(b, "ci", "battery", "Upload junit", with_={"path": "junit-*.txt"}),
        "junit-*.xml",
    ),
    (
        RuleId.CI_JUNIT_SPLIT_ORDER,
        lambda b: _drop_run_step(b, "ci", "coverage", "scripts.split_junit junit/battery"),
        "split exactly once",
    ),
    (
        RuleId.CI_COVERAGE_DOWNLOAD_GUARDS,
        lambda b: _mutate_job_step(b, "ci", "coverage", "Download test-browser junit", if_="${{ always() }}"),
        "result != 'skipped'",
    ),
    (RuleId.CI_NO_DIRECT_CODECOV, _add_direct_codecov, "directly to Codecov"),
    (RuleId.RELEASE_TRIGGERS, lambda b: _add_trigger(b, "release", "workflow_run"), "push and dispatch"),
    (RuleId.RELEASE_NO_FAIL_FAST_MATRIX, _set_fail_fast, "fail-fast"),
    (RuleId.RELEASE_VALIDATE_BEFORE_UPLOAD, _drop_validation_gate, "not gated on validate_all"),
)


@pytest.mark.parametrize(
    ("rule_id", "mutate", "hint"), MUTATIONS, ids=[f"{m[0].value}-{m[2]}" for m in MUTATIONS]
)
def test_a_config_rule_names_the_injected_violation(rule_id: RuleId, mutate, hint: str) -> None:
    rule = next(rule for rule in RULES if rule.id is rule_id)
    findings = list(rule.check(mutate(LIVE)))
    assert findings, f"rule {rule_id} did not flag the {hint} evasion"
    assert any(finding.rule is rule_id and hint in finding.message for finding in findings)


def test_every_policy_id_has_a_bite() -> None:
    assert {rule_id for rule_id, _, _ in MUTATIONS} == set(RuleId)
