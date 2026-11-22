"""Contract tests for nightly.yml workflow."""

from __future__ import annotations

import yaml

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "nightly.yml"
PYTEST_INI_PATH = SERVICE_ROOT / "pytest.ini"
PYPROJECT_PATH = SERVICE_ROOT / "pyproject.toml"
RUN_TESTS_ACTION_PATH = WORKFLOW_PATH.parent.parent / "actions" / "run-tests" / "action.yml"


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


def _get_pytest_markers() -> dict[str, str]:
    """Parse pytest.ini and return the markers dict."""
    import configparser

    config = configparser.ConfigParser()
    config.read(PYTEST_INI_PATH)
    if "pytest" not in config:
        return {}
    markers_raw = config["pytest"].get("markers", "")
    markers = {}
    for line in markers_raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: "marker_name: description"
        if ":" in line:
            name, desc = line.split(":", 1)
            markers[name.strip()] = desc.strip()
    return markers


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


def test_nightly_xvfb_prepare_browser_camoufox() -> None:
    """nightly.yml has prepare-browser; the door action handles camoufox/fpgen/xvfb."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    test_job = jobs.get("test")
    assert isinstance(test_job, dict), "test job required"

    steps = test_job.get("steps")
    assert isinstance(steps, list), "test job must have steps"

    prepare_browser = any(
        "prepare-browser" in str(step.get("uses", "")) for step in steps if isinstance(step, dict)
    )
    door = any("run-tests" in str(step.get("uses", "")) for step in steps if isinstance(step, dict))
    assert prepare_browser, "prepare-browser action required"
    assert door, "nightly test job must call the run-tests door"

    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    assert "cache" in action and "camoufox" in action, "the door must cache camoufox"
    assert "camoufox fetch" in action, "the door must fetch camoufox"
    assert "ensure_fpgen_model" in action, "the door must install the fpgen model"
    assert "xvfb-run" in action, "the door must run pytest under xvfb-run"


def test_nightly_env_no_proxy_headless_false() -> None:
    """nightly.yml must set NO_PROXY and CRAWLUTRON_HEADLESS=false for browser tests."""
    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    assert "NO_PROXY: 127.0.0.1,localhost" in action, "bad NO_PROXY in door action"
    assert 'CRAWLUTRON_HEADLESS: "false"' in action, "bad headless in door action"


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
                if isinstance(step, dict) and "scripts.coverage_reports" in step.get("run", ""):
                    return

    if coverage_job:
        steps = coverage_job.get("steps", [])
        for step in steps:
            if isinstance(step, dict) and "scripts.coverage_reports" in step.get("run", ""):
                return

    raise AssertionError("scripts.coverage_reports step required for per-package reports")


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


def test_nightly_coverage_source_pkgs_not_overridden() -> None:
    """nightly.yml must not override coverage source_pkgs; the door uses --cov=crawlutron only."""
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    expected_source_pkgs = data.get("tool", {}).get("coverage", {}).get("run", {}).get("source_pkgs", [])
    assert isinstance(expected_source_pkgs, list), "source_pkgs must be a list"
    assert len(expected_source_pkgs) == 7, (
        f"expected 7 source_pkgs, got {len(expected_source_pkgs)}: {expected_source_pkgs}"
    )

    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    import re

    cov_matches = re.findall(r"--cov=([^\s]+)", action)
    assert set(cov_matches) == {"crawlutron"}, f"expected --cov=crawlutron only, got {cov_matches}"
    assert "--cov-source" not in action, "must not override --cov-source"
    assert "--cov-config" not in action, "must not override --cov-config"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
