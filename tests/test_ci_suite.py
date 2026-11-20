"""Suite rerun tests: the battery hook runs the whole default suite."""

from __future__ import annotations

import re

import pytest

from tests._ci_contract import WORKFLOW_PATH, _job_run_steps


def _assert_no_default_suite_rerun(workflow: str) -> None:
    """The check above, callable on a mutated workflow so the probe can bite.

    Only pytest's own args count: the interpreter's ``-m`` is not a marker
    filter, so a bare ``pytest`` and a ``-m browser`` run are told apart.
    """
    for name, run in _job_run_steps(workflow):
        if "pytest" not in run:
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
