"""Contract tests for nightly.yml workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "nightly.yml"


def workflow_document() -> dict[object, object]:
    """Load and parse the nightly workflow."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "nightly.yml is not a workflow"
    return document


def _get_on_block(document: dict[object, object]) -> dict[str, object]:
    """Get the 'on' block, handling YAML boolean key parsing."""
    on_block = document.get("on") or document.get(True)
    assert isinstance(on_block, dict), "on block must be a mapping"
    return on_block


def test_nightly_workflow_exists() -> None:
    """nightly.yml must exist."""
    assert WORKFLOW_PATH.exists(), "nightly.yml does not exist"


def test_nightly_triggers_schedule() -> None:
    """nightly.yml must trigger on daily schedule."""
    doc = workflow_document()
    on_block = _get_on_block(doc)

    schedule = on_block.get("schedule")
    assert isinstance(schedule, list) and schedule, "daily schedule trigger required"
    cron = schedule[0].get("cron")
    assert cron == "0 3 * * *", f"daily cron must be 3am UTC, got {cron}"

    # Should also have workflow_dispatch for manual runs
    dispatch = on_block.get("workflow_dispatch")
    # PyYAML parses empty workflow_dispatch: as None
    assert dispatch is not None, "workflow_dispatch trigger required"


def test_nightly_no_m_filter() -> None:
    """nightly.yml must run full test suite WITHOUT -m filter (slow+browser+real_pbkdf2 all run)."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    test_job = jobs.get("test")
    assert isinstance(test_job, dict), "test job required"

    steps = test_job.get("steps")
    assert isinstance(steps, list), "test job must have steps"

    # Find the pytest run step - check both run and env PYTEST_ADDOPTS
    pytest_step = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        env = step.get("env", {})
        pytest_addopts = env.get("PYTEST_ADDOPTS", "")
        if "pytest" in run:
            pytest_step = step
            break

    assert pytest_step is not None, "pytest run step required"
    run_cmd = pytest_step.get("run", "")
    env = pytest_step.get("env", {})
    pytest_addopts = env.get("PYTEST_ADDOPTS", "")
    # The run command uses 'python -m pytest' which has -m for module, not pytest's -m filter
    # Check that pytest's -m filter is not used (would appear as 'pytest -m "..."' or 'pytest -m ...')
    import re

    pytest_m_filter = re.search(r"pytest\s+-m\s+", run_cmd)
    assert not pytest_m_filter, "nightly run command must NOT use pytest's -m filter"
    # PYTEST_ADDOPTS should have --cov and --cov-branch but NOT -m filter
    assert "--cov" in pytest_addopts, "pytest must run with --cov"
    assert "--cov-branch" in pytest_addopts, "pytest must run with --cov-branch"
    # Check for pytest -m filter in PYTEST_ADDOPTS
    pytest_addopts_m_filter = re.search(r"-m\s+", pytest_addopts)
    assert not pytest_addopts_m_filter, "nightly must not use -m filter in PYTEST_ADDOPTS"


def test_nightly_xvfb_prepare_browser_camoufox() -> None:
    """nightly.yml must have xvfb, prepare-browser action, and camoufox cache/fetch/fpgen."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    test_job = jobs.get("test")
    assert isinstance(test_job, dict), "test job required"

    steps = test_job.get("steps")
    assert isinstance(steps, list), "test job must have steps"

    # Check for prepare-browser action
    prepare_browser = False
    camoufox_cache = False
    camoufox_fetch = False
    fpgen_model = False
    xvfb_run = False

    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        run = step.get("run", "")
        name = step.get("name", "").lower()

        if "prepare-browser" in uses:
            prepare_browser = True
        if "cache" in uses and "camoufox" in name:
            camoufox_cache = True
        if "camoufox fetch" in run:
            camoufox_fetch = True
        if "fpgen" in run or "ensure_fpgen_model" in run:
            fpgen_model = True
        if "xvfb-run" in run:
            xvfb_run = True

    assert prepare_browser, "prepare-browser action required"
    assert camoufox_cache, "camoufox cache step required"
    assert camoufox_fetch, "camoufox fetch step required"
    assert fpgen_model, "fpgen model install step required"
    assert xvfb_run, "xvfb-run required for browser tests"


def test_nightly_env_no_proxy_headless_false() -> None:
    """nightly.yml must set NO_PROXY and CRAWLUTRON_HEADLESS=false for browser tests."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    test_job = jobs.get("test")
    assert isinstance(test_job, dict), "test job required"

    steps = test_job.get("steps")
    assert isinstance(steps, list), "test job must have steps"

    # Find the pytest run step and check env
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        if "xvfb-run" in run and "pytest" in run:
            env = step.get("env")
            assert isinstance(env, dict), "pytest step must have env block"
            no_proxy = env.get("NO_PROXY")
            headless = env.get("CRAWLUTRON_HEADLESS")
            assert no_proxy == "127.0.0.1,localhost", f"bad NO_PROXY: {no_proxy}"
            assert headless == "false", f"bad headless: {headless}"
            return

    raise AssertionError("pytest step with xvfb-run and env not found")


def test_nightly_per_package_reports() -> None:
    """nightly.yml must generate per-package reports via scripts/coverage_reports.py."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    # Should have a coverage job or step
    coverage_job = jobs.get("coverage")
    if not coverage_job:
        # Check if it's a step in test job
        test_job = jobs.get("test")
        if test_job:
            steps = test_job.get("steps", [])
            for step in steps:
                if isinstance(step, dict) and "coverage_reports.py" in step.get("run", ""):
                    return

    if coverage_job:
        steps = coverage_job.get("steps", [])
        for step in steps:
            if isinstance(step, dict) and "coverage_reports.py" in step.get("run", ""):
                return

    raise AssertionError("coverage_reports.py step required for per-package reports")


def test_nightly_artifacts() -> None:
    """nightly.yml must upload artifacts (coverage reports, junit)."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    # Check for artifact uploads
    has_coverage_artifact = False
    has_junit_artifact = False

    for _job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses", "")
            if "upload-artifact" in uses:
                name = step.get("name", "").lower()
                with_block = step.get("with", {})
                path = with_block.get("path", "")
                if "coverage" in name or "coverage" in path:
                    has_coverage_artifact = True
                if "junit" in name or "junit" in path or "xml" in path:
                    has_junit_artifact = True

    assert has_coverage_artifact, "coverage artifact upload required"
    assert has_junit_artifact, "junit artifact upload required"


def test_nightly_flag_nightly() -> None:
    """nightly.yml must have a 'nightly' flag/label for identification."""
    doc = workflow_document()
    # Check workflow name or job name contains nightly
    raw_name = doc.get("name", "")
    name: str = raw_name if isinstance(raw_name, str) else ""
    assert "nightly" in name.lower(), f"workflow name must contain 'nightly', got {name}"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
