"""Every rule is proved to fail on an injected violation."""

from __future__ import annotations

import pytest

from tests.workflows._model import (
    JOBS,
    Job,
    Step,
    Workflow,
    parent_workflow,
    workflow,
)
from tests.workflows._policy import RuleId
from tests.workflows._rules import (
    JOB_RULES,
    WORKFLOW_RULES,
    codeql_no_build_step,
    codeql_registry_pin,
    codeql_security_events_write,
    codeql_security_extended,
    codeql_single_sarif_upload,
    door_no_if,
    needs_resolve,
    nightly_artifacts,
    nightly_coverage_reports,
    nightly_door_call,
    no_bare_pytest,
    no_event_splicing_in_run,
    no_heredoc,
    no_pip_install,
    unique_step_ids,
    uses_tests_door,
    weekly_artifacts,
    weekly_lowest_direct,
    weekly_mutation_targets,
    weekly_mutation_timebox,
    weekly_mutation_tool,
)

_SAMPLE: Job = next(job for job in JOBS if job.steps)


def _mutate(**changes) -> Job:
    return _SAMPLE.model_copy(update=changes)


def _job_from(stem: str, key: str) -> Job:
    return workflow(stem).jobs[key]


def _codeql_analyze() -> Job:
    return _job_from("codeql", "analyze")


def _nightly_test() -> Job:
    return _job_from("nightly", "test")


def _weekly_mutation() -> Job:
    return _job_from("weekly", "mutation-testing")


def _weekly_lowest() -> Job:
    return _job_from("weekly", "lowest-direct-resolution")


def _step(**changes) -> Step:
    raw = {**_SAMPLE.steps[0].model_dump(by_alias=True), **changes}
    if "run" in changes:
        raw.pop("uses", None)
    if "uses" in changes:
        raw.pop("run", None)
    return Step.create(raw)


@pytest.mark.parametrize(
    ("rule", "broken"),
    [
        (needs_resolve, _mutate(needs=("ghost-job",))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{ github.event.x }}"),))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{github.event.x}}"),))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{ (github.event.x) }}"),))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{ tojson(github.event) }}"),))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{ github['event'].x }}"),))),
        (no_bare_pytest, _mutate(steps=(_step(name="Bite", run="python -m pytest"),))),
        (
            no_bare_pytest,
            _mutate(steps=(_step(name="Bite", run="pytest", env={"UNRELATED": "-m not slow"}),)),
        ),
        (no_bare_pytest, _mutate(steps=(_step(name="Bite", run='git commit -m "msg" && pytest'),))),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip install x"),))),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip3 install x"),))),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip --no-cache-dir install x"),))),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip \\\n  --no-cache-dir install x"),))),
        (no_heredoc, _mutate(steps=(_step(run="cat <<EOF"),))),
        (no_heredoc, _mutate(steps=(_step(run="cat <<'PY' > x.py"),))),
        (no_heredoc, _mutate(steps=(_step(run="python <<PYCODE"),))),
        (
            unique_step_ids,
            _mutate(steps=(_step(id="same"), _step(id="same"))),
        ),
        (
            codeql_security_events_write,
            _codeql_analyze().model_copy(update={"permissions": {}}),
        ),
        (
            codeql_registry_pin,
            _codeql_analyze().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"uses": step.uses.split("@")[0] + "@" + "0" * 40})
                        if step.uses and "codeql-action" in step.uses
                        else step
                        for step in _codeql_analyze().steps
                    )
                }
            ),
        ),
        (
            codeql_security_extended,
            _codeql_analyze().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"with_": {"queries": "default"}})
                        if step.uses and "codeql-action/init" in step.uses
                        else step
                        for step in _codeql_analyze().steps
                    )
                }
            ),
        ),
        (
            codeql_no_build_step,
            _codeql_analyze().model_copy(
                update={
                    "steps": tuple(
                        list(_codeql_analyze().steps)
                        + [
                            Step.create(
                                {
                                    "name": "Build",
                                    "run": "python -m build",
                                    "line": 99,
                                }
                            )
                        ]
                    )
                }
            ),
        ),
        (
            codeql_single_sarif_upload,
            _codeql_analyze().model_copy(
                update={
                    "steps": tuple(
                        list(_codeql_analyze().steps)
                        + [
                            Step.create(
                                {
                                    "name": "Upload SARIF",
                                    "uses": f"github/codeql-action/upload-sarif@{'0' * 40}",
                                    "line": 100,
                                }
                            )
                        ]
                    )
                }
            ),
        ),
        (
            nightly_door_call,
            _nightly_test().model_copy(
                update={
                    "steps": tuple(
                        step for step in _nightly_test().steps if not (step.uses and "run-tests" in step.uses)
                    )
                },
            ),
        ),
        (
            nightly_coverage_reports,
            _nightly_test().model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _nightly_test().steps
                        if not (step.run and "scripts.coverage_reports" in step.run)
                    )
                },
            ),
        ),
        (
            weekly_mutation_timebox,
            _weekly_mutation().model_copy(update={"timeout_minutes": None}),
        ),
        (
            weekly_mutation_tool,
            _weekly_mutation().model_copy(
                update={
                    "steps": tuple(
                        step for step in _weekly_mutation().steps if not (step.run and "mutmut" in step.run)
                    )
                },
            ),
        ),
        (
            weekly_mutation_targets,
            _weekly_mutation().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(
                            update={
                                "run": (
                                    (step.run or "")
                                    .replace("invariants", "other")
                                    .replace("admission", "other")
                                    .replace("core", "other")
                                )
                            }
                        )
                        if step.run and "mutmut run" in step.run
                        else step
                        for step in _weekly_mutation().steps
                    )
                }
            ),
        ),
        (
            weekly_lowest_direct,
            _weekly_lowest().model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _weekly_lowest().steps
                        if not (step.run and "uv pip compile" in step.run)
                    )
                },
            ),
        ),
    ],
)
def test_a_rule_names_the_injected_violation(rule, broken) -> None:
    parent = parent_workflow(broken)
    findings = list(rule(parent, broken))
    assert findings
    assert findings[0].workflow == broken.workflow
    assert findings[0].job == broken.key
    assert findings[0].rule == rule.__name__.replace("_", "-")
    assert parent.path.name in findings[0].message


_WORKFLOW_BY_RULE = {
    "codeql-triggers": "codeql",
    "nightly-triggers": "nightly",
    "nightly-name": "nightly",
    "nightly-concurrency": "nightly",
    "weekly-triggers": "weekly",
    "ci-packaging-read-only": "packaging",
    "ci-security-permissions": "ci",
}

_WORKFLOW_MUTATIONS: dict[str, dict[str, object]] = {
    "codeql-triggers": {"on_": {}},
    "nightly-triggers": {"on_": {}},
    "weekly-triggers": {"on_": {}},
    "declares-permissions": {"permissions": {}},
    "has-trigger": {"on_": {}},
    "nightly-name": {"name": "CI"},
    "nightly-concurrency": {"concurrency": None},
    "ci-packaging-read-only": {"permissions": {}},
    "ci-security-permissions": {"permissions": {}},
}


@pytest.mark.parametrize(
    ("rule", "broken"),
    [
        (
            rule,
            workflow(_WORKFLOW_BY_RULE.get(rule.name, "ci")).model_copy(
                update=_WORKFLOW_MUTATIONS.get(rule.name, {"on_": {}})
            ),
        )
        for rule in WORKFLOW_RULES
        if rule.name in _WORKFLOW_MUTATIONS
    ],
)
def test_a_workflow_rule_names_the_injected_violation(rule, broken: Workflow) -> None:
    findings = list(rule.check(broken))
    assert findings and findings[0].rule == rule.name


@pytest.mark.parametrize(
    ("rule", "stem", "rule_id"),
    [
        (nightly_artifacts, "nightly", RuleId.NIGHTLY_ARTIFACTS.value),
        (weekly_artifacts, "weekly", RuleId.WEEKLY_ARTIFACTS.value),
    ],
)
def test_artifact_bites_names_missing_artifacts(rule, stem, rule_id) -> None:
    wf = workflow(stem)
    jobs = {
        key: job.model_copy(
            update={"steps": tuple(s for s in job.steps if "upload-artifact" not in (s.uses or ""))}
        )
        for key, job in wf.jobs.items()
    }
    broken = wf.model_copy(update={"jobs": jobs})
    findings = list(rule(broken))
    assert findings
    assert findings[0].rule == rule_id


def test_an_escaped_unquoted_export_value_is_honoured() -> None:
    filtered = _mutate(steps=(_step(name="Bite", run="export PYTEST_ADDOPTS=-m\\ slow; pytest"),))
    assert not list(no_bare_pytest(parent_workflow(filtered), filtered))


def test_quoted_parentheses_do_not_change_scope() -> None:
    for run in (
        '(export PYTEST_ADDOPTS="-m slow)"; pytest)',
        "(export PYTEST_ADDOPTS=\"-m 'not (browser or slow)'\"; pytest)",
        'then (export PYTEST_ADDOPTS="-m slow"; pytest)',
    ):
        filtered = _mutate(steps=(_step(name="Bite", run=run),))
        assert not list(no_bare_pytest(parent_workflow(filtered), filtered)), run


def test_an_export_inside_the_same_subshell_filters_its_pytest() -> None:
    scoped = _mutate(steps=(_step(name="Bite", run='(export PYTEST_ADDOPTS="-m slow"; pytest)'),))
    assert not list(no_bare_pytest(parent_workflow(scoped), scoped))


def test_an_export_only_filters_subsequent_invocations() -> None:
    ordered = _mutate(steps=(_step(name="Bite", run='pytest; export PYTEST_ADDOPTS="-m slow"; pytest'),))
    assert len(list(no_bare_pytest(parent_workflow(ordered), ordered))) == 1


def test_a_nested_quote_export_value_is_honoured() -> None:
    filtered = _mutate(
        steps=(
            _step(
                name="Bite",
                run="export PYTEST_ADDOPTS=\"-m 'not (browser or slow)'\"; pytest",
            ),
        )
    )
    assert not list(no_bare_pytest(parent_workflow(filtered), filtered))


def test_two_pytest_invocations_yield_two_findings() -> None:
    mixed = _mutate(steps=(_step(name="Bite", run="pytest -m slow; pytest"),))
    assert len(list(no_bare_pytest(parent_workflow(mixed), mixed))) == 1
    broken = _mutate(steps=(_step(name="Bite", run="pytest; pytest"),))
    assert len(list(no_bare_pytest(parent_workflow(broken), broken))) == 2


def test_a_job_with_a_direct_pytest_must_use_the_door() -> None:
    broken = _mutate(steps=(_step(name="Bite", run="pytest"),))
    assert list(uses_tests_door(parent_workflow(broken), broken))


def test_a_door_step_without_if_is_clean() -> None:
    clean = _mutate(
        steps=(
            _step(name="Door", uses="$/.github/actions/run-tests"),
            _step(name="Prove", run="true"),
        )
    )
    assert not list(door_no_if(parent_workflow(clean), clean))


def test_a_door_step_with_if_false_is_refused() -> None:
    """Planting step-level if:false on the door must be named file:line."""
    door = _step(name="Door", uses="$/.github/actions/run-tests").model_copy(update={"if": "${{ false }}"})
    broken = _mutate(steps=(door, _step(name="Prove", run="true")))
    findings = list(door_no_if(parent_workflow(broken), broken))
    assert findings
    assert findings[0].rule == "door-no-if"
    assert parent_workflow(broken).path.name in findings[0].message


def test_a_direct_pytest_under_if_false_is_still_bare_pytest() -> None:
    """`if: false` does not bypass the sealed no-bare-pytest rule."""
    base = _step(name="Bite", run="pytest")
    broken = _mutate(steps=(base.model_copy(update={"if": "${{ false }}"}),))
    assert list(no_bare_pytest(parent_workflow(broken), broken))


def test_an_inline_marker_filter_is_not_a_bare_suite_run() -> None:
    filtered = _mutate(steps=(_step(name="Bite", run="PYTEST_ADDOPTS='-m slow' pytest"),))
    assert not list(no_bare_pytest(parent_workflow(filtered), filtered))


def test_every_job_rule_has_a_bite() -> None:
    assert {rule.check.__name__.replace("_", "-") for rule in JOB_RULES} == {
        "needs-resolve",
        "no-event-splicing-in-run",
        "no-bare-pytest",
        "uses-tests-door",
        "door-no-if",
        "no-pip-install",
        "no-heredoc",
        "no-silent-uploads",
        "unique-step-ids",
        "codeql-security-events-write",
        "codeql-registry-pin",
        "codeql-security-extended",
        "codeql-no-build-step",
        "codeql-single-sarif-upload",
        "nightly-door-call",
        "nightly-coverage-reports",
        "weekly-mutation-timebox",
        "weekly-mutation-tool",
        "weekly-mutation-targets",
        "weekly-lowest-direct",
        "ci-secret-scan-fetch-depth",
        "ci-dependency-review-checkout",
        "ci-dependency-review-refs",
        "ci-real-pbkdf2-door",
        "ci-actionlint-hook",
        "ci-browser-fpgen-model",
        "ci-bytecode-compileall",
        "ci-coverage-needs",
        "ci-coverage-if-always",
        "ci-coverage-download-artifacts",
        "ci-coverage-upload-hidden",
        "ci-coverage-junit-upload",
        "ci-coverage-distinct-junit-paths",
        "ci-coverage-uploads-coverage-xml",
        "ci-coverage-single-artifact",
        "ci-gate-jobs-start-from-prepare",
    }


def test_every_workflow_rule_has_a_bite() -> None:
    assert {rule.check.__name__.replace("_", "-") for rule in WORKFLOW_RULES} == {
        "declares-permissions",
        "has-trigger",
        "codeql-triggers",
        "nightly-triggers",
        "nightly-name",
        "nightly-concurrency",
        "weekly-triggers",
        "nightly-artifacts",
        "weekly-artifacts",
        "ci-packaging-read-only",
        "ci-security-permissions",
        "ci-release-gated-on-ci",
    }
