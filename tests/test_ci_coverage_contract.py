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


def _get_coverage_job(workflow: str) -> StepMapping:
    """Return the coverage job dict, asserting it exists."""
    jobs = _jobs(workflow)
    coverage_job = jobs.get("coverage")
    assert coverage_job is not None
    return cast(StepMapping, coverage_job)


def _codecov_upload_steps(workflow: str, job_name: str) -> list[StepMapping]:
    """Return codecov upload steps for a job."""
    steps = job_steps(workflow, job_name)
    return [
        step
        for step in steps
        if isinstance(step, dict) and "uses" in step and "codecov" in str(step.get("uses", ""))
    ]


def _artifact_upload_steps(workflow: str, job_name: str) -> list[StepMapping]:
    """Return artifact upload steps for a job."""
    steps = job_steps(workflow, job_name)
    return [
        step
        for step in steps
        if isinstance(step, dict) and "uses" in step and "upload-artifact" in str(step.get("uses", ""))
    ]


def _download_artifact_steps(workflow: str, job_name: str) -> list[StepMapping]:
    """Return artifact download steps for a job."""
    steps = job_steps(workflow, job_name)
    return [
        step
        for step in steps
        if isinstance(step, dict) and "uses" in step and "download-artifact" in str(step.get("uses", ""))
    ]


def _assert_codecov_upload_flags(
    with_: StepMapping,
    job_name: str,
    require_flags: bool = True,
    report_type: str = "test_results",
) -> None:
    """Assert a Codecov upload carries the expected flags and report type."""
    if require_flags:
        assert "flags" in with_, f"{job_name} codecov upload must specify flags"
    assert with_.get("use_oidc") is True or with_.get("use_oidc") == "true"
    assert with_.get("fail_ci_if_error") is False or with_.get("fail_ci_if_error") == "false"
    assert "report-type" not in with_, "action input is report_type (underscore), not report-type"
    assert with_.get("report_type") == report_type


def test_coverage_job_depends_on_battery_real_pbkdf2_test_browser() -> None:
    """Coverage needs the change filter plus the three test jobs."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    needs = _get_coverage_job(workflow).get("needs")
    assert isinstance(needs, list)
    assert set(needs) == {"changes", "battery", "real-pbkdf2", "test-browser"}


def test_coverage_job_runs_if_always() -> None:
    """Coverage job must have if: always() so partial failure still reports."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    coverage_job = _get_coverage_job(workflow)
    assert coverage_job.get("if") == "${{ always() }}", "coverage job must run if: always()"


def test_coverage_job_downloads_artifacts_tolerantly() -> None:
    """Browser artifacts are downloaded only when the browser suite actually ran."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    download_steps = _download_artifact_steps(workflow, "coverage")
    names = [cast(StepMapping, step.get("with", {})).get("name") for step in download_steps]
    assert "coverage-data" in names
    assert "junit-results" in names
    assert "coverage-data-real-pbkdf2" in names
    assert "coverage-data-test-browser" in names
    for step in download_steps:
        name = cast(StepMapping, step.get("with", {})).get("name")
        if "test-browser" in str(name):
            expected = "${{ always() && needs.changes.outputs.browser == 'true' }}"
        elif "real-pbkdf2" in str(name):
            expected = "${{ always() && needs.changes.outputs.real-pbkdf2 == 'true' }}"
        else:
            expected = "${{ always() }}"
        assert step.get("if") == expected, f"download step {step.get('name')} must use {expected}"


def _assert_coverage_data_upload(workflow: str, job_name: str, artifact_name: str) -> None:
    """Assert a hidden coverage-data artifact is uploaded with hidden files included."""
    uploads = [
        step
        for step in _artifact_upload_steps(workflow, job_name)
        if str(cast(StepMapping, step.get("with", {})).get("name", "")) == artifact_name
    ]
    assert len(uploads) == 1, f"{job_name} must upload exactly one {artifact_name} artifact"
    with_ = cast(StepMapping, uploads[0].get("with", {}))
    assert with_.get("path") == ".coverage*", f"{job_name} coverage upload must use .coverage*"
    assert with_.get("include-hidden-files") is True, (
        f"{job_name} must set include-hidden-files: true or coverage data is silently dropped"
    )


def test_battery_uploads_hidden_coverage_data() -> None:
    """Battery uploads .coverage* with hidden files included."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_coverage_data_upload(workflow, "battery", "coverage-data")


def test_real_pbkdf2_uploads_hidden_coverage_data() -> None:
    """real-pbkdf2 uploads .coverage* with hidden files included."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_coverage_data_upload(workflow, "real-pbkdf2", "coverage-data-real-pbkdf2")


def test_test_browser_uploads_hidden_coverage_data() -> None:
    """test-browser uploads .coverage* with hidden files included."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_coverage_data_upload(workflow, "test-browser", "coverage-data-test-browser")


def _assert_junit_upload(workflow: str, job_name: str, artifact_name: str, artifact_path: str) -> None:
    """Assert a job uploads a junit artifact with correct name and path."""
    upload_steps = _artifact_upload_steps(workflow, job_name)
    junit_uploads = [
        step
        for step in upload_steps
        if "junit" in str(cast(StepMapping, step.get("with", {})).get("name", ""))
    ]
    assert len(junit_uploads) == 1, f"{job_name} must upload exactly one junit artifact"
    with_ = cast(StepMapping, junit_uploads[0].get("with", {}))
    assert with_.get("name") == artifact_name, f"{job_name} junit artifact must be named {artifact_name}"
    assert with_.get("path") == artifact_path, f"{job_name} must upload {artifact_path}"
    assert with_.get("if-no-files-found") == "warn"
    assert junit_uploads[0].get("if") == "${{ always() }}", f"{job_name} junit upload must have if: always()"


def test_battery_uploads_junit_artifact() -> None:
    """Battery job uploads junit-*.xml artifact named junit-results."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_junit_upload(workflow, "battery", "junit-results", "junit-*.xml")


def test_real_pbkdf2_uploads_junit_artifact() -> None:
    """real-pbkdf2 job uploads junit-real-pbkdf2.xml artifact named junit-real-pbkdf2."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_junit_upload(workflow, "real-pbkdf2", "junit-real-pbkdf2", "junit-real-pbkdf2.xml")


def test_test_browser_uploads_junit_artifact() -> None:
    """test-browser job uploads junit-browser.xml artifact named junit-test-browser."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_junit_upload(workflow, "test-browser", "junit-test-browser", "junit-browser.xml")


def test_coverage_job_downloads_junit_artifacts_to_distinct_paths() -> None:
    """Coverage job downloads junit artifacts into distinct paths."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    download_steps = _download_artifact_steps(workflow, "coverage")
    junit_downloads = [
        step
        for step in download_steps
        if "junit" in str(cast(StepMapping, step.get("with", {})).get("name", ""))
    ]
    # Should download junit-results (battery), junit-real-pbkdf2, junit-test-browser
    names = [cast(StepMapping, step.get("with", {})).get("name") for step in junit_downloads]
    assert "junit-results" in names
    assert "junit-real-pbkdf2" in names
    assert "junit-test-browser" in names
    # Check distinct paths
    paths = {}
    for step in junit_downloads:
        with_ = cast(StepMapping, step.get("with", {}))
        name = with_.get("name")
        path = with_.get("path", ".")
        paths[name] = path
    assert paths["junit-results"] == "junit/battery"
    assert paths["junit-real-pbkdf2"] == "junit/real-pbkdf2"
    assert paths["junit-test-browser"] == "junit/test-browser"


def test_coverage_job_reads_the_browser_change_filter() -> None:
    """Coverage must see the change-filter output gating the browser suite."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    needs = _get_coverage_job(workflow)["needs"]
    assert isinstance(needs, list)
    assert "changes" in needs


def test_coverage_job_uploads_per_surface_codecov_with_explicit_files() -> None:
    """Coverage job uploads per-surface Codecov with explicit per-surface files."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    upload_steps = _codecov_upload_steps(workflow, "coverage")
    test_results_uploads = [
        step
        for step in upload_steps
        if "junit" in str(cast(StepMapping, step.get("with", {})).get("files", ""))
    ]
    assert len(test_results_uploads) == 4, (
        "coverage job must have 4 test_results uploads (behavioral, invariants, real-pbkdf2, test-browser)"
    )

    expected_files = {
        "behavioral": "junit/split/behavioral/junit-*.xml",
        "invariants": "junit/split/invariants/junit-*.xml",
        "real-pbkdf2": "junit/real-pbkdf2/junit-real-pbkdf2.xml",
        "test-browser": "junit/test-browser/junit-browser.xml",
    }

    for step in test_results_uploads:
        with_ = cast(StepMapping, step.get("with", {}))
        files = str(with_.get("files", ""))
        flag = with_.get("flags")
        assert flag in expected_files, f"unknown flag {flag}"
        assert files == expected_files[flag], f"flag {flag} expects files={expected_files[flag]}, got {files}"
        assert "report-type" not in with_, "action input is report_type (underscore), not report-type"
        assert with_.get("report_type") == "test_results"
        expected_if = (
            "${{ !cancelled() && needs.changes.outputs.real-pbkdf2 == 'true' }}"
            if flag == "real-pbkdf2"
            else "${{ !cancelled() }}"
        )
        assert step.get("if") == expected_if


def test_coverage_job_splits_battery_junit_before_upload() -> None:
    """Battery JUnit is split once, before the two flagged Codecov uploads."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "coverage")
    split_indexes = [
        index
        for index, step in enumerate(steps)
        if "scripts.split_junit junit/battery junit/split" in str(step.get("run", ""))
    ]
    assert len(split_indexes) == 1
    upload_indexes = [
        index
        for index, step in enumerate(steps)
        if "junit/split/" in str(cast(StepMapping, step.get("with", {})).get("files", ""))
    ]
    assert len(upload_indexes) == 2
    assert all(index > split_indexes[0] for index in upload_indexes)


def test_no_direct_codecov_upload_in_real_pbkdf2_and_test_browser() -> None:
    """real-pbkdf2 and test-browser must not have direct Codecov uploads (coverage job owns it)."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for job_name in ("real-pbkdf2", "test-browser"):
        upload_steps = _codecov_upload_steps(workflow, job_name)
        assert len(upload_steps) == 0, f"{job_name} must not have direct Codecov uploads"


def test_codecov_yml_has_six_components_and_informational_status() -> None:
    """codecov.yml has six components via component_management and
    informational status; no blocking threshold."""
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

    # Check component_management (not flag_management)
    component_mgmt = cast(StepMapping, config.get("component_management", {}))
    assert (
        cast(
            bool,
            cast(StepMapping, component_mgmt.get("default_rules", {})).get("carryforward"),
        )
        is True
    ), "component_management.default_rules.carryforward must be true"

    # Check six components via individual_components
    individual_components = cast(StepMapping, component_mgmt.get("individual_components", {}))
    expected_components = {"battery", "real-pbkdf2", "test-browser", "admission", "crawl", "sdk"}
    assert set(individual_components.keys()) == expected_components, (
        f"individual_components = {set(individual_components.keys())}, expected {expected_components}"
    )

    # Surface components (battery, real-pbkdf2, test-browser) use flag_regexes
    for surface in ("battery", "real-pbkdf2", "test-browser"):
        comp = cast(StepMapping, individual_components[surface])
        assert "flag_regexes" in comp, f"component {surface} must have flag_regexes"
        assert comp["flag_regexes"] == [surface], f"component {surface} flag_regexes must be [{surface!r}]"
        assert comp.get("carryforward") is True, f"component {surface} must have carryforward: true"

    # Package components (admission, crawl, sdk) use paths
    for package in ("admission", "crawl", "sdk"):
        comp = cast(StepMapping, individual_components[package])
        assert "paths" in comp, f"component {package} must have paths"
        assert isinstance(comp["paths"], list) and len(comp["paths"]) > 0, (
            f"component {package} paths must be non-empty list"
        )
        assert comp.get("carryforward") is True, f"component {package} must have carryforward: true"

    # Ensure flag_management is NOT present (deprecated in favor of component_management)
    assert "flag_management" not in config, "codecov.yml must use component_management, not flag_management"


def test_coverage_job_artifact_path_is_coverage_reports() -> None:
    """Coverage job uploads artifact from coverage_reports/ (not coverage_report/)."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    upload_steps = _artifact_upload_steps(workflow, "coverage")
    coverage_report_uploads = [
        step
        for step in upload_steps
        if "coverage" in str(cast(StepMapping, step.get("with", {})).get("name", ""))
    ]
    assert len(coverage_report_uploads) == 1, "coverage job must upload exactly one coverage report artifact"
    with_ = cast(StepMapping, coverage_report_uploads[0].get("with", {}))
    assert with_.get("path") == "coverage_reports/", (
        f"coverage artifact path must be coverage_reports/, got {with_.get('path')}"
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
        "crawlutron",
        "crawlutron_admit",
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


def test_coverage_job_uploads_generated_coverage_xml() -> None:
    """The report job sends the generated coverage XML to Codecov."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "coverage")
    uploads = [
        step
        for step in steps
        if isinstance(step, dict)
        and "codecov" in str(step.get("uses", ""))
        and "coverage.xml" in str(cast(StepMapping, step.get("with", {})).get("files", ""))
    ]
    assert uploads, "coverage job must upload coverage_reports/coverage.xml to Codecov"
    for step in uploads:
        with_ = cast(StepMapping, step.get("with", {}))
        assert "coverage_reports/coverage.xml" in str(with_.get("files", ""))
        assert "report-type" not in with_, "action input is report_type (underscore), not report-type"
        assert with_.get("report_type", "coverage") == "coverage"
        assert step.get("if") == "${{ !cancelled() }}"


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
    assert len(artifact_steps) == 1, f"coverage job must emit one artifact, got {len(artifact_steps)}"


def test_coverage_producers_use_distinct_coverage_files() -> None:
    """Each producer names its data file so coverage combine can merge all three."""
    from tests._ci_contract import WORKFLOW_PATH as _WP

    action = (_WP.parent.parent / "actions" / "run-tests" / "action.yml").read_text(encoding="utf-8")
    assert "coverage-file:" in action, "the door must declare coverage-file as an input"
    assert "${{ inputs.coverage-file }}" in action, "the door must reference the coverage-file input"
    workflow = _WP.read_text(encoding="utf-8")
    expected = (
        ("battery", ".coverage.battery"),
        ("real-pbkdf2", ".coverage.real-pbkdf2"),
        ("test-browser", ".coverage.test-browser"),
    )
    for job_name, file_name in expected:
        assert f"coverage-file: {file_name}" in workflow, (
            f"{job_name} must pass coverage-file={file_name} to the door"
        )
