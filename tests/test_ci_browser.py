"""Browser job tests: the browser job runs only browser-marked tests."""

from __future__ import annotations

from tests._ci_contract import WORKFLOW_PATH, job_steps


def test_browser_job_selects_only_browser_marked_tests() -> None:
    """The browser job exists to run the browser suite: a filter that admits
    anything else re-runs the fast suite (~3,400 tests) on every push."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    browser_step = str(
        next(
            step.get("run")
            for step in steps
            if isinstance(step, dict) and "Run pytest (browser)" == str(step.get("name"))
        )
    )
    assert browser_step.strip() == 'xvfb-run -a .venv/bin/python -m pytest -m "browser"', browser_step


def test_browser_job_installs_pinned_fpgen_model_before_tests() -> None:
    """A warm camoufox cache skips its fetch, the only fpgen model installer."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    pytest_index = next(i for i, step in enumerate(steps) if str(step.get("name")) == "Run pytest (browser)")
    installers = [
        (i, step) for i, step in enumerate(steps) if "ensure_fpgen_model" in str(step.get("run", ""))
    ]
    assert len(installers) == 1, installers
    index, installer = installers[0]
    assert index < pytest_index, "the model must be installed before pytest starts"
    assert "if" not in installer, "a warm browser cache must not skip the model install"
    assert "camoufox.fpgen_model" in str(installer["run"])
    assert "python -m fpgen fetch" not in str(installer["run"])
