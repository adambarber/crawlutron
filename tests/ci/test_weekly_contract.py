"""Contract tests for weekly.yml workflow."""

from __future__ import annotations

import yaml

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "weekly.yml"
PYPROJECT_PATH = SERVICE_ROOT / "pyproject.toml"


def workflow_document() -> dict[object, object]:
    """Load and parse the weekly workflow."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "weekly.yml is not a workflow"
    return document


def _get_on_block(document: dict[object, object]) -> dict[str, object]:
    """Get the 'on' block, handling YAML boolean key parsing."""
    on_block = document.get("on") or document.get(True)
    assert isinstance(on_block, dict), "on block must be a mapping"
    return on_block


def _get_source_pkgs_from_pyproject() -> list[str]:
    """Derive the expected source_pkgs from pyproject.toml."""
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    source_pkgs = data.get("tool", {}).get("coverage", {}).get("run", {}).get("source_pkgs", [])
    assert isinstance(source_pkgs, list), "source_pkgs must be a list"
    return [str(pkg) for pkg in source_pkgs]


def test_weekly_workflow_exists() -> None:
    """weekly.yml must exist."""
    assert WORKFLOW_PATH.exists(), "weekly.yml does not exist"


def test_weekly_triggers_schedule() -> None:
    """weekly.yml must trigger on weekly schedule."""
    doc = workflow_document()
    on_block = _get_on_block(doc)

    schedule = on_block.get("schedule")
    assert isinstance(schedule, list) and schedule, "weekly schedule trigger required"
    cron = schedule[0].get("cron")
    assert cron == "0 4 * * 0", f"weekly cron must be Sunday 4am UTC, got {cron}"

    # Should also have workflow_dispatch for manual runs
    dispatch = on_block.get("workflow_dispatch")
    # PyYAML parses empty workflow_dispatch: as None
    assert dispatch is not None, "workflow_dispatch trigger required"


def test_weekly_mutation_testing_timeboxed() -> None:
    """weekly.yml must run timeboxed mutation testing on invariant-critical modules with HTML artifact."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    mutation_job = jobs.get("mutation-testing")
    assert isinstance(mutation_job, dict), "mutation-testing job required"

    # Check timeout is set (timeboxed)
    timeout = mutation_job.get("timeout-minutes")
    assert isinstance(timeout, int), "mutation-testing job must have timeout-minutes"
    assert timeout <= 120, f"mutation-testing timeout must be <= 120 minutes, got {timeout}"

    steps = mutation_job.get("steps")
    assert isinstance(steps, list), "mutation-testing job must have steps"

    # Check for mutmut or similar mutation testing tool
    has_mutation_tool = False
    has_html_artifact = False

    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        uses = step.get("uses", "")
        name = step.get("name", "").lower()

        if "mutmut" in run or "mutation" in run or "mutmut" in uses:
            has_mutation_tool = True
        if "upload-artifact" in uses:
            with_block = step.get("with", {})
            path = with_block.get("path", "")
            if "html" in path or "mutation" in name:
                has_html_artifact = True

    assert has_mutation_tool, "mutation testing tool (mutmut) step required"
    assert has_html_artifact, "HTML artifact upload required for mutation testing"


def test_weekly_mutation_invariant_critical_modules() -> None:
    """weekly.yml must target invariant-critical modules for mutation testing."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    mutation_job = jobs.get("mutation-testing")
    assert isinstance(mutation_job, dict), "mutation-testing job required"

    steps = mutation_job.get("steps")
    assert isinstance(steps, list), "mutation-testing job must have steps"

    # Check that mutation testing targets invariant-critical modules
    # (tests/invariants/, core admission kernel, etc.)
    found_mutmut_run = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        if "mutmut run" in run:
            found_mutmut_run = True
            # Should specify paths to mutate
            assert "invariants" in run or "admission" in run or "core" in run, (
                "mutation testing must target invariant-critical modules"
            )
            return

    assert found_mutmut_run, "mutmut run step not found"
    raise AssertionError("mutation testing step targeting invariant-critical modules not found")


def test_weekly_lowest_direct_resolution_probe() -> None:
    """weekly.yml must run lowest-direct resolution probe."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    resolution_job = jobs.get("lowest-direct-resolution")
    assert isinstance(resolution_job, dict), "lowest-direct-resolution job required"

    steps = resolution_job.get("steps")
    assert isinstance(steps, list), "lowest-direct-resolution job must have steps"

    has_compile = False
    has_clean_venv = False
    has_import_check = False

    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        name = step.get("name", "").lower()

        if "uv pip compile" in run and "--resolution lowest-direct" in run:
            has_compile = True
        if "venv" in run and ("clean" in name or "fresh" in name or "new" in name):
            has_clean_venv = True
        if "import" in run and ("crawlutron" in run or "admission" in run or "safe_url" in run):
            has_import_check = True

    assert has_compile, "uv pip compile --resolution lowest-direct step required"
    assert has_clean_venv, "clean venv creation step required"
    assert has_import_check, "import every member verification step required"


def test_weekly_import_check_matches_source_pkgs() -> None:
    """weekly.yml lowest-direct probe must import exactly the source_pkgs from pyproject.toml."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    resolution_job = jobs.get("lowest-direct-resolution")
    assert isinstance(resolution_job, dict), "lowest-direct-resolution job required"

    steps = resolution_job.get("steps")
    assert isinstance(steps, list), "lowest-direct-resolution job must have steps"

    # Find the import verification step
    import_step = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        if "import" in run and ("crawlutron" in run or "admission" in run or "safe_url" in run):
            import_step = step
            break

    assert import_step is not None, "import verification step required"

    # Derive expected packages from pyproject.toml
    expected_packages = _get_source_pkgs_from_pyproject()

    # Extract the packages list from the run command
    run_cmd = import_step.get("run", "")
    # The packages list is in a Python list literal in the run command
    import re

    match = re.search(r"packages\s*=\s*\[(.*?)\]", run_cmd, re.DOTALL)
    assert match, f"could not find packages list in import step: {run_cmd}"

    # Parse the package names from the list literal
    packages_str = match.group(1)
    # Extract quoted strings
    found_packages = re.findall(r"['\"]([^'\"]+)['\"]", packages_str)
    # Normalize: replace hyphens with underscores for import
    found_packages = [pkg.replace("-", "_") for pkg in found_packages]

    # The workflow uses a hand-maintained list; it must match the derived source_pkgs
    assert set(found_packages) == set(expected_packages), (
        f"workflow import list {sorted(found_packages)} does not match "
        f"pyproject.toml source_pkgs {sorted(expected_packages)}"
    )


def test_weekly_artifact_upload() -> None:
    """weekly.yml must upload mutation testing HTML artifact and resolution probe results."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    has_mutation_artifact = False
    has_resolution_artifact = False

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
                if "mutation" in name or "html" in path:
                    has_mutation_artifact = True
                if "resolution" in name or "lowest" in name or "probe" in name:
                    has_resolution_artifact = True

    assert has_mutation_artifact, "mutation testing HTML artifact upload required"
    assert has_resolution_artifact, "resolution probe results artifact upload required"


def test_mutmut_in_dev_dependencies() -> None:
    """pyproject.toml must include mutmut==3.2.0 in [project.optional-dependencies] dev."""
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)

    dev_deps = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    assert isinstance(dev_deps, list), "dev dependencies must be a list"

    # Check for mutmut==3.2.0
    mutmut_found = any(dep.startswith("mutmut==3.2.0") for dep in dev_deps)
    assert mutmut_found, f"mutmut==3.2.0 not found in dev dependencies: {dev_deps}"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
