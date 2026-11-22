"""Every rule is proved to fail on an injected violation."""

from __future__ import annotations

import pytest

from tests.workflows._model import JOBS, WORKFLOWS, Job, Workflow, parent_workflow
from tests.workflows._rules import (
    JOB_RULES,
    WORKFLOW_RULES,
    needs_resolve,
    no_bare_pytest,
    no_event_splicing_in_run,
    no_heredoc,
    no_pip_install,
    unique_step_ids,
)

_SAMPLE: Job = next(job for job in JOBS if job.steps)


def _mutate(**changes) -> Job:
    return _SAMPLE.model_copy(update=changes)


def _step(**changes):
    return _SAMPLE.steps[0].model_copy(update=changes)


@pytest.mark.parametrize(
    ("rule", "broken"),
    [
        (needs_resolve, _mutate(needs=("ghost-job",))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{ github.event.x }}"),))),
        (no_event_splicing_in_run, _mutate(steps=(_step(run="echo ${{github.event.x}}"),))),
        (no_bare_pytest, _mutate(steps=(_step(name="Bite", run="python -m pytest"),))),
        (
            no_bare_pytest,
            _mutate(steps=(_step(name="Bite", run="pytest", env={"UNRELATED": "-m not slow"}),)),
        ),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip install x"),))),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip3 install x"),))),
        (no_pip_install, _mutate(steps=(_step(name="Bite", run="pip --no-cache-dir install x"),))),
        (no_heredoc, _mutate(steps=(_step(run="cat <<EOF"),))),
        (no_heredoc, _mutate(steps=(_step(run="cat <<'PY' > x.py"),))),
        (no_heredoc, _mutate(steps=(_step(run="python <<PYCODE"),))),
        (
            unique_step_ids,
            _mutate(steps=(_step(id="same"), _step(id="same"))),
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


@pytest.mark.parametrize(
    ("rule", "broken"),
    [
        (
            rule,
            WORKFLOWS[0].model_copy(update={"permissions": {}})
            if rule.name == "declares-permissions"
            else WORKFLOWS[0].model_copy(update={"on_": {}}),
        )
        for rule in WORKFLOW_RULES
    ],
)
def test_a_workflow_rule_names_the_injected_violation(rule, broken: Workflow) -> None:
    findings = list(rule.check(broken))
    assert findings and findings[0].rule == rule.name


def test_an_inline_marker_filter_is_not_a_bare_suite_run() -> None:
    filtered = _mutate(steps=(_step(name="Bite", run="PYTEST_ADDOPTS='-m slow' pytest"),))
    assert not list(no_bare_pytest(parent_workflow(filtered), filtered))


def test_every_job_rule_has_a_bite() -> None:
    assert {rule.check.__name__.replace("_", "-") for rule in JOB_RULES} == {
        "needs-resolve",
        "no-event-splicing-in-run",
        "no-bare-pytest",
        "no-pip-install",
        "no-heredoc",
        "unique-step-ids",
    }
