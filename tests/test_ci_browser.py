"""Browser job tests: the browser job runs only browser-marked tests."""

from __future__ import annotations

from tests._ci_contract import WORKFLOW_PATH, job_steps


def test_browser_job_selects_only_browser_marked_tests() -> None:
    """Browser job runs browser suite only on PRs; all markers on main push."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    # PR step: only browser marker
    pr_step = str(
        next(
            step.get("run")
            for step in steps
            if isinstance(step, dict) and "Run pytest (browser, PRs only)" == str(step.get("name"))
        )
    )
    assert '-m "browser"' in pr_step, pr_step
    assert "--cov=crawlutron" in pr_step, pr_step
    assert "--cov-report=" in pr_step, pr_step
    assert "--cov-append" in pr_step, pr_step
    assert "xvfb-run -a .venv/bin/python -m pytest" in pr_step, pr_step
    # Main push/release step: all markers (unconditional)
    main_name = "Run pytest (all markers, main push and release calls)"
    matching_steps = [step for step in steps if isinstance(step, dict) and main_name == str(step.get("name"))]
    main_step = str(matching_steps[0].get("run"))
    assert '-m "browser"' not in main_step, main_step
    assert "--cov=crawlutron" in main_step, main_step
    assert "--cov-report=" in main_step, main_step
    assert "--cov-append" in main_step, main_step
    assert "xvfb-run -a .venv/bin/python -m pytest" in main_step, main_step


def test_browser_job_installs_pinned_fpgen_model_before_tests() -> None:
    """Warm camoufox cache skips fetch; model installer must run first."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    # Both pytest steps must come after the model installer
    pytest_indices = [
        i
        for i, step in enumerate(steps)
        if isinstance(step, dict) and str(step.get("name", "")).startswith("Run pytest")
    ]
    installers = [
        (i, step) for i, step in enumerate(steps) if "ensure_fpgen_model" in str(step.get("run", ""))
    ]
    assert len(installers) == 1, installers
    index, installer = installers[0]
    assert all(index < pi for pi in pytest_indices), "the model must be installed before all pytest steps"
    assert "if" not in installer, "a warm browser cache must not skip the model install"
    assert "camoufox.fpgen_model" in str(installer["run"])
    assert "python -m fpgen fetch" not in str(installer["run"])
