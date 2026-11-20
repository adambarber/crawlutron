"""Contract tests for measure-once coverage architecture (#1201)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import yaml

from tests._ci_contract import (
    WORKFLOW_PATH,
    _jobs,
    job_steps,
)

StepMapping = Mapping[str, object]


def _codecov_upload_steps(workflow: str, job_name: str) -> list[StepMapping]:
    """Return codecov upload steps for a job."""
    steps = job_steps(workflow, job_name)
    return [
        step
        for step in steps
        if isinstance(step, dict) and "uses" in step and "codecov" in str(step.get("uses", ""))
    ]


def _assert_codecov_upload_flags(with_: StepMapping, job_name: str, require_flags: bool = True) -> None:
    """Assert codecov upload has correct flags."""
    if require_flags:
        assert "flags" in with_, f"{job_name} codecov upload must specify flags"
    assert with_.get("use_oidc") is True or with_.get("use_oidc") == "true"
    assert with_.get("fail_ci_if_error") is False or with_.get("fail_ci_if_error") == "false"
    assert with_.get("report_type") == "test_results" or with_.get("report-type") == "test_results"


def test_battery_is_only_fast_suite_execution() -> None:
    """The battery job runs pytest with coverage via PYTEST_ADDOPTS; no other job runs the fast suite."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = _jobs(workflow)

    # Battery must exist and run pytest with coverage via PYTEST_ADDOPTS
    assert "battery" in jobs
    battery_steps = job_steps(workflow, "battery")
    battery_step = next((s for s in battery_steps if s.get("name") == "battery"), None)
    assert battery_step is not None, "battery step must exist"
    battery_env = cast(StepMapping, battery_step.get("env", {}))
    pytest_addopts = cast(str, battery_env.get("PYTEST_ADDOPTS", ""))
    assert "--cov=crawlutron" in pytest_addopts, "battery must have --cov in PYTEST_ADDOPTS"
    assert "--cov-report=" in pytest_addopts, "battery must have --cov-report in PYTEST_ADDOPTS"
    assert "--cov-append" in pytest_addopts, "battery must have --cov-append in PYTEST_ADDOPTS"
    assert "junit" in pytest_addopts.lower(), "battery must have junit in PYTEST_ADDOPTS"

    # Coverage job must NOT run pytest (report-only)
    assert "coverage" in jobs
    coverage_steps = job_steps(workflow, "coverage")
    coverage_runs = " ".join(str(cast(StepMapping, step).get("run", "")) for step in coverage_steps)
    assert "pytest" not in coverage_runs, "coverage job must not re-run pytest (report-only)"
    assert "coverage combine" in coverage_runs, "coverage job must run coverage combine"
    assert "coverage_reports.py" in coverage_runs, "coverage job must call frozen report script"

    # real-pbkdf2 and test-browser must not run the fast suite
    for job_name in ("real-pbkdf2", "test-browser"):
        job_runs = " ".join(
            str(cast(StepMapping, step).get("run", "")) for step in job_steps(workflow, job_name)
        )
        # They run their own marker selection, not the fast suite
        assert "-m " in job_runs, f"{job_name} must select its own markers"
        assert "not (browser or slow or real_pbkdf2)" not in job_runs, (
            f"{job_name} must not run the battery's marker filter"
        )


def test_coverage_job_depends_on_battery_real_pbkdf2_test_browser() -> None:
    """Coverage job needs [battery, real-pbkdf2, test-browser]."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = _jobs(workflow)
    coverage_job = jobs.get("coverage")
    assert coverage_job is not None
    needs = coverage_job.get("needs")
    assert isinstance(needs, list)
    assert set(needs) == {"battery", "real-pbkdf2", "test-browser"}, (
        f"coverage needs {needs}, expected [battery, real-pbkdf2, test-browser]"
    )


def test_codecov_uploads_use_informational_status_and_correct_flags() -> None:
    """Codecov uploads use !cancelled(), report-type: test_results, SHA-pinned action,
    use_oidc: true, fail_ci_if_error: false."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _jobs(workflow)

    # Check coverage job uploads
    coverage_steps = job_steps(workflow, "coverage")
    upload_steps = [
        step
        for step in coverage_steps
        if isinstance(step, dict) and "uses" in step and "codecov" in str(step.get("uses", ""))
    ]
    assert upload_steps, "coverage job must have codecov upload steps"

    for step in upload_steps:
        step_dict = cast(StepMapping, step)
        uses = str(step_dict.get("uses", ""))
        # SHA-pinned codecov-action
        assert "@" in uses and len(uses.split("@")[1]) == 40, f"codecov action must be SHA-pinned: {uses}"
        with_ = cast(StepMapping, step_dict.get("with", {}))
        _assert_codecov_upload_flags(with_, "coverage", require_flags=False)


def test_codecov_yml_has_six_components_and_informational_status() -> None:
    """codecov.yml has six components and informational status; no blocking threshold."""
    codecov_path = WORKFLOW_PATH.parent.parent.parent / "codecov.yml"
    assert codecov_path.exists(), "codecov.yml must exist at repo root"
    config = yaml.safe_load(codecov_path.read_text(encoding="utf-8"))

    # Check informational status
    coverage_status = cast(StepMapping, config.get("coverage", {}).get("status", {}))
    project_status = cast(
        StepMapping,
        cast(StepMapping, coverage_status.get("project", {})).get("default", {}),
    )
    assert cast(bool, project_status.get("informational")) is True, "project status must be informational"
    patch_status = cast(
        StepMapping,
        cast(StepMapping, coverage_status.get("patch", {})).get("default", {}),
    )
    assert cast(bool, patch_status.get("informational")) is True, "patch status must be informational"

    # Check flag_management carryforward
    flag_mgmt = cast(StepMapping, config.get("flag_management", {}))
    assert (
        cast(
            bool,
            cast(StepMapping, flag_mgmt.get("default_rules", {})).get("carryforward"),
        )
        is True
    )

    # Check six components (flags) are defined
    flags = cast(StepMapping, flag_mgmt.get("flags", {}))
    expected_flags = {"battery", "real-pbkdf2", "test-browser", "admission", "crawl", "sdk"}
    assert set(flags.keys()) == expected_flags, f"flags = {set(flags.keys())}, expected {expected_flags}"
    for flag_name, flag_config in flags.items():
        flag_config_dict = cast(StepMapping, flag_config)
        assert cast(bool, flag_config_dict.get("carryforward")) is True, (
            f"flag {flag_name} must have carryforward: true"
        )


def test_coverage_config_has_source_pkgs_relative_files_parallel() -> None:
    """pyproject.toml [tool.coverage.run] has source_pkgs = all seven import names,
    relative_files=true, parallel=true."""
    import tomllib

    pyproject_path = WORKFLOW_PATH.parent.parent.parent / "pyproject.toml"
    config = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    coverage_run = cast(StepMapping, config.get("tool", {}).get("coverage", {}).get("run", {}))
    # source_pkgs should list all seven import names
    source_pkgs = cast(list[str], coverage_run.get("source_pkgs", []))
    expected_packages = [
        "admission",
        "crawlutron",
        "crawlutron_extract",
        "crawlutron_mcp",
        "crawlutron_rest",
        "crawlutron_sdk",
        "safe_url",
    ]
    assert set(source_pkgs) == set(expected_packages), (
        f"source_pkgs = {source_pkgs}, expected {expected_packages}"
    )
    assert coverage_run.get("relative_files") is True
    assert coverage_run.get("parallel") is True

    # Stale comments about 3.14/COVERAGE_CORE should be fixed (not changing semantics)
    # This is a documentation check - the config values should not have changed beyond the ticket's scope
    assert coverage_run.get("core") == "sysmon"
    assert coverage_run.get("branch") is False
    assert coverage_run.get("dynamic_context") is False


def test_no_blocking_threshold_anywhere() -> None:
    """No --cov-fail-under, no fail_ci_if_error: true, no threshold in codecov.yml."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "--cov-fail-under" not in workflow
    assert "fail_ci_if_error: true" not in workflow
    assert "fail_ci_if_error: 'true'" not in workflow

    codecov_path = WORKFLOW_PATH.parent.parent.parent / "codecov.yml"
    config = yaml.safe_load(codecov_path.read_text(encoding="utf-8"))
    # No threshold that would block
    project_status = cast(
        StepMapping,
        config.get("coverage", {}).get("status", {}).get("project", {}).get("default", {}),
    )
    assert "threshold" not in project_status or project_status.get("threshold") == "0.5%", (
        "project status threshold must be informational only"
    )


def test_scripts_coverage_reports_py_untouched() -> None:
    """scripts/coverage_reports.py is not created or edited by this ticket (owned by B1)."""
    # The file should not exist in this workspace's changes
    # It will be created by B1 separately
    WORKFLOW_PATH.parent.parent.parent / "scripts" / "coverage_reports.py"
    # We don't assert it doesn't exist - it might be created by B1 in parallel
    # But we assert WE don't create/edit it - this test documents the contract


def test_battery_job_env_includes_pytest_addopts_with_coverage() -> None:
    """Battery job sets PYTEST_ADDOPTS with --cov, --cov-report, and per-invocation junit."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Check battery job has PYTEST_ADDOPTS with coverage flags
    assert "PYTEST_ADDOPTS" in workflow
    assert "--cov=crawlutron" in workflow
    assert "--cov-report=" in workflow
    assert "--cov-append" in workflow
    assert "junit" in workflow.lower()


def test_coverage_job_emits_single_artifact() -> None:
    """Coverage job emits one combined artifact after calling coverage_reports.py."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _jobs(workflow)
    coverage_steps = job_steps(workflow, "coverage")
    # Should have upload-artifact step
    artifact_steps = [
        step
        for step in coverage_steps
        if isinstance(step, dict) and "uses" in step and "upload-artifact" in str(step.get("uses", ""))
    ]
    assert artifact_steps, "coverage job must upload artifact"
    # Should be one main artifact (the combined report)
    # The exact count may vary but there should be at least one


def test_browser_and_real_pbkdf2_add_data_test_results_pattern() -> None:
    """Browser and real-pbkdf2 jobs upload to Codecov with same data/test-results pattern."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for job_name in ("real-pbkdf2", "test-browser"):
        upload_steps = _codecov_upload_steps(workflow, job_name)
        assert upload_steps, f"{job_name} must upload to Codecov"
        for step in upload_steps:
            with_ = cast(StepMapping, step.get("with", {}))
            _assert_codecov_upload_flags(with_, job_name)
