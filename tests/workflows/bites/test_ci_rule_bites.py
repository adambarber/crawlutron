"""CI rule bites: every CI rule fails on its injected violation."""

from __future__ import annotations

import pytest

from tests.workflows._model import Job, parent_workflow, workflow
from tests.workflows._rules import (
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


def _job_from(stem: str, key: str) -> Job:
    return workflow(stem).jobs[key]


def _ci_secret_scan() -> Job:
    return _job_from("ci", "secret-scan")


def _ci_dep_review() -> Job:
    return _job_from("ci", "dependency-review")


def _ci_real_pbkdf2() -> Job:
    return _job_from("ci", "real-pbkdf2")


def _ci_actionlint() -> Job:
    return _job_from("ci", "actionlint")


def _ci_test_browser() -> Job:
    return _job_from("ci", "test-browser")


def _ci_battery() -> Job:
    return _job_from("ci", "battery")


def _ci_coverage() -> Job:
    return _job_from("ci", "coverage")


@pytest.mark.parametrize(
    ("rule", "broken"),
    [
        (
            ci_secret_scan_fetch_depth,
            _ci_secret_scan().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"with_": {"fetch-depth": "1"}})
                        if step.uses and step.uses.startswith("actions/checkout@")
                        else step
                        for step in _ci_secret_scan().steps
                    )
                }
            ),
        ),
        (
            ci_dependency_review_checkout,
            _ci_dep_review().model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _ci_dep_review().steps
                        if not (step.uses and step.uses.startswith("actions/checkout@"))
                    )
                },
            ),
        ),
        (
            ci_dependency_review_refs,
            _ci_dep_review().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"with_": {"base-ref": "refs/heads/main"}})
                        if step.uses and "dependency-review-action@" in step.uses
                        else step
                        for step in _ci_dep_review().steps
                    )
                }
            ),
        ),
        (
            ci_real_pbkdf2_door,
            _ci_real_pbkdf2().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"if_": "${{ false }}"})
                        if step.uses and "run-tests" in step.uses
                        else step
                        for step in _ci_real_pbkdf2().steps
                    )
                }
            ),
        ),
        (
            ci_actionlint_hook,
            _ci_actionlint().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"run": ""})
                        if step.run and ".venv/bin/pre-commit run actionlint --all-files" in step.run
                        else step
                        for step in _ci_actionlint().steps
                    )
                }
            ),
        ),
        (
            ci_browser_fpgen_model,
            _ci_test_browser().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"uses": None})
                        if step.uses and "run-tests" in step.uses
                        else step
                        for step in _ci_test_browser().steps
                    )
                }
            ),
        ),
        (
            ci_bytecode_compileall,
            _ci_battery().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"run": ""}) if step.run and "compileall" in step.run else step
                        for step in _ci_battery().steps
                    )
                }
            ),
        ),
        (
            ci_coverage_needs,
            _ci_coverage().model_copy(update={"needs": ("battery",)}),
        ),
        (
            ci_coverage_if_always,
            _ci_coverage().model_copy(update={"if_": "${{ false }}"}),
        ),
        (
            ci_coverage_download_artifacts,
            _ci_coverage().model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _ci_coverage().steps
                        if not (step.uses and "download-artifact" in step.uses)
                    )
                }
            ),
        ),
        (
            ci_coverage_upload_hidden,
            _ci_battery().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(
                            update={"with_": {"path": ".coverage*", "include-hidden-files": False}}
                        )
                        if step.uses
                        and "upload-artifact" in step.uses
                        and str(step.with_.get("path", "")) == ".coverage*"
                        else step
                        for step in _ci_battery().steps
                    )
                }
            ),
        ),
        (
            ci_coverage_junit_upload,
            _ci_battery().model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _ci_battery().steps
                        if not (
                            step.uses
                            and "upload-artifact" in step.uses
                            and "junit" in str(step.with_.get("name", "")).lower()
                        )
                    )
                }
            ),
        ),
        (
            ci_coverage_distinct_junit_paths,
            _ci_coverage().model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"with_": {**step.with_, "path": "junit/shared"}})
                        if step.uses
                        and "download-artifact" in step.uses
                        and "junit" in str(step.with_.get("name", "")).lower()
                        else step
                        for step in _ci_coverage().steps
                    )
                }
            ),
        ),
        (
            ci_coverage_uploads_coverage_xml,
            _ci_coverage().model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _ci_coverage().steps
                        if not (
                            step.uses
                            and "codecov" in str(step.uses)
                            and "coverage.xml" in str(step.with_.get("files", ""))
                        )
                    )
                }
            ),
        ),
        (
            ci_coverage_single_artifact,
            _ci_coverage().model_copy(
                update={
                    "steps": tuple(
                        list(_ci_coverage().steps)
                        + [
                            step.model_copy(update={"name": "Extra artifact"})
                            for step in _ci_coverage().steps
                            if step.uses and "upload-artifact" in step.uses
                        ][:1]
                    )
                }
            ),
        ),
        (
            ci_gate_jobs_start_from_prepare,
            _job_from("ci", "ruff").model_copy(
                update={
                    "steps": tuple(
                        step
                        for step in _job_from("ci", "ruff").steps
                        if not (step.uses and str(step.uses).startswith("$/.github/actions/prepare"))
                    )
                }
            ),
        ),
    ],
)
def test_ci_rule_names_the_injected_violation(rule, broken) -> None:
    parent = parent_workflow(broken)
    findings = list(rule(parent, broken))
    assert findings
    assert findings[0].workflow == broken.workflow
    assert findings[0].job == broken.key
    assert findings[0].rule == rule.__name__.replace("_", "-")
    assert parent.path.name in findings[0].message
