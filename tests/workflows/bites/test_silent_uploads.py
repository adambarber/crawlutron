"""Every silent-upload token is named file:line by the no-silent-uploads rule."""

from __future__ import annotations

import pytest

from tests.workflows._model import JOBS, Job, Step, parent_workflow
from tests.workflows._rules import no_silent_uploads

_SAMPLE: Job = next(job for job in JOBS if job.steps)
_ALIAS = {"if_": "if", "with_": "with", "continue_on_error": "continue-on-error"}
_CODECOV = "codecov/codecov-action@" + "a" * 40
_LOOKALIKE = "evil-codecov/codecov-action@" + "b" * 40


def _mutate(**changes) -> Job:
    return _SAMPLE.model_copy(update=changes)


def _step(**changes) -> Step:
    raw = {**_SAMPLE.steps[0].model_dump(by_alias=True)}
    for key, value in changes.items():
        raw[_ALIAS.get(key, key)] = value
    if "run" in changes:
        raw.pop("uses", None)
    if "uses" in changes:
        raw.pop("run", None)
    return Step.create(raw)


def _codecov_step(**changes) -> Step:
    raw = {
        "uses": _CODECOV,
        "name": "Upload coverage XML to Codecov",
        "line": 9,
        "with": {"fail_ci_if_error": True, "files": "coverage_reports/coverage.xml"},
    }
    for key, value in changes.items():
        raw[_ALIAS.get(key, key)] = value
    return Step.create(raw)


@pytest.mark.parametrize(
    ("broken",),
    [
        (_mutate(steps=(_step(with_={"fail_ci_if_error": False}),)),),
        (_mutate(steps=(_step(with_={"if-no-files-found": "warn"}),)),),
        (
            _mutate(
                steps=(
                    _step(
                        uses="actions/download-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
                        if_="${{ always() && needs.changes.outputs.browser == 'true' }}",
                    ),
                )
            ),
        ),
    ],
)
def test_a_silent_upload_token_is_named(broken) -> None:
    parent = parent_workflow(broken)
    findings = list(no_silent_uploads(parent, broken))
    assert findings
    assert findings[0].rule == "no-silent-uploads"
    assert findings[0].workflow == broken.workflow
    assert findings[0].job == broken.key
    assert parent.path.name in findings[0].message


def test_a_fail_ci_if_error_true_step_is_clean() -> None:
    clean = _mutate(steps=(_step(with_={"fail_ci_if_error": True}),))
    assert not list(no_silent_uploads(parent_workflow(clean), clean))


def test_an_if_no_files_found_error_step_is_clean() -> None:
    clean = _mutate(steps=(_step(with_={"if-no-files-found": "error"}),))
    assert not list(no_silent_uploads(parent_workflow(clean), clean))


def test_a_download_gated_on_producer_result_is_clean() -> None:
    clean = _mutate(
        steps=(
            _step(
                uses="actions/download-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
                if_="${{ always() && needs.test-browser.result != 'skipped' }}",
            ),
        )
    )
    assert not list(no_silent_uploads(parent_workflow(clean), clean))


def test_codecov_with_fail_ci_if_error_false_is_clean() -> None:
    clean = _SAMPLE.model_copy(update={"steps": (_codecov_step(with_={"fail_ci_if_error": False}),)})
    assert not list(no_silent_uploads(parent_workflow(clean), clean))


def test_non_codecov_with_fail_ci_if_error_false_reds_file_line() -> None:
    broken = _mutate(steps=(_step(with_={"fail_ci_if_error": False}),))
    parent = parent_workflow(broken)
    findings = list(no_silent_uploads(parent, broken))
    assert findings
    assert findings[0].rule == "no-silent-uploads"
    assert str(broken.steps[0].line) in findings[0].message


def test_lookalike_codecov_with_fail_ci_if_error_false_reds() -> None:
    step = Step.__new__(Step)
    object.__setattr__(step, "uses", _LOOKALIKE)
    object.__setattr__(step, "name", "Upload coverage XML to Codecov")
    object.__setattr__(step, "line", 9)
    object.__setattr__(step, "run", None)
    object.__setattr__(step, "event_spliced", False)
    object.__setattr__(
        step,
        "with_",
        {"fail_ci_if_error": False, "files": "coverage_reports/coverage.xml"},
    )
    job = _SAMPLE.model_copy(update={"steps": (step,), "key": "coverage", "workflow": "ci"})
    parent = parent_workflow(job)
    findings = list(no_silent_uploads(parent, job))
    assert findings
    assert findings[0].rule == "no-silent-uploads"
    assert str(step.line) in findings[0].message


def test_codecov_with_if_no_files_found_warn_reds() -> None:
    step = Step.__new__(Step)
    object.__setattr__(step, "uses", _CODECOV)
    object.__setattr__(step, "name", "Upload coverage XML to Codecov")
    object.__setattr__(step, "line", 9)
    object.__setattr__(step, "run", None)
    object.__setattr__(step, "event_spliced", False)
    object.__setattr__(
        step,
        "with_",
        {"fail_ci_if_error": True, "if-no-files-found": "warn"},
    )
    job = _SAMPLE.model_copy(update={"steps": (step,), "key": "coverage", "workflow": "ci"})
    parent = parent_workflow(job)
    findings = list(no_silent_uploads(parent, job))
    assert findings
    assert findings[0].rule == "no-silent-uploads"
    assert str(step.line) in findings[0].message
