"""Residual contract tests for nightly.yml not covered by the typed workflow rules."""

from __future__ import annotations

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "nightly.yml"
PYPROJECT_PATH = SERVICE_ROOT / "pyproject.toml"
RUN_TESTS_ACTION_PATH = WORKFLOW_PATH.parent.parent / "actions" / "run-tests" / "action.yml"


def workflow_document() -> dict[object, object]:
    """Load and parse the nightly workflow."""
    import yaml

    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "nightly.yml is not a workflow"
    return document


def test_nightly_workflow_exists() -> None:
    """nightly.yml must exist."""
    assert WORKFLOW_PATH.exists(), "nightly.yml does not exist"


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


def test_nightly_coverage_source_pkgs_not_overridden() -> None:
    """nightly.yml must not override coverage source_pkgs; the door uses --cov=crawlutron only."""
    import re
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    expected_source_pkgs = data.get("tool", {}).get("coverage", {}).get("run", {}).get("source_pkgs", [])
    assert isinstance(expected_source_pkgs, list), "source_pkgs must be a list"
    assert len(expected_source_pkgs) == 7, (
        f"expected 7 source_pkgs, got {len(expected_source_pkgs)}: {expected_source_pkgs}"
    )

    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    cov_matches = re.findall(r"--cov=([^\s]+)", action)
    assert set(cov_matches) == {"crawlutron"}, f"expected --cov=crawlutron only, got {cov_matches}"
    assert "--cov-source" not in action, "must not override --cov-source"
    assert "--cov-config" not in action, "must not override --cov-config"
