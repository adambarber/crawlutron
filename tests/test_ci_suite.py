"""Suite rerun tests: the battery hook runs the whole default suite."""

from __future__ import annotations

import re

import pytest

from tests._ci_contract import WORKFLOW_PATH, _job_run_steps


def _assert_no_default_suite_rerun(workflow: str) -> None:
    """Check no job reruns default suite; test-browser main push is excepted."""
    import yaml

    workflow_doc = yaml.safe_load(workflow)
    jobs = workflow_doc.get("jobs", {})
    test_browser_job = jobs.get("test-browser", {})
    test_browser_steps = test_browser_job.get("steps", [])
    # Find the main push step's run command
    main_push_name = "Run pytest (all markers, main push and release calls)"
    main_push_run = None
    for step in test_browser_steps:
        if isinstance(step, dict) and step.get("name") == main_push_name:
            main_push_run = step.get("run")
            break

    for name, run in _job_run_steps(workflow):
        if "pytest" not in run:
            continue
        # Allow test-browser main push step to run full suite
        if name == "test-browser" and run == main_push_run:
            continue
        pytest_args = run.split("pytest", 1)[1]
        assert re.search(r"(?:^|\s)-m(?:\s|=)", pytest_args), (
            f"job {name} runs the default suite ({run.strip()!r}): enforcement's "
            "battery hook already runs it; give a second suite its own marker filter"
        )


def test_no_ci_job_reruns_the_default_suite() -> None:
    """`enforcement`'s battery hook already runs the whole default suite, so a
    bare `pytest` in another job is a second copy: the two can drift, and every
    push pays twice. A `-m` selection is a different surface, and may stand.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_no_default_suite_rerun(workflow)


def test_a_ci_job_that_reruns_the_default_suite_is_caught() -> None:
    """Bites: re-adding a bare-pytest job fails the check."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    needle = "  test-browser:"
    inserted = (
        "  test-fast:\n"
        "    name: Test suite (fast)\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Run pytest (fast)\n"
        "        run: .venv/bin/python -m pytest\n\n"
        "  test-browser:"
    )
    broken = workflow.replace(needle, inserted)
    assert broken != workflow, "the injection did not apply; the probe proves nothing"
    with pytest.raises(AssertionError):
        _assert_no_default_suite_rerun(broken)
