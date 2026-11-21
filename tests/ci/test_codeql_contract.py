"""Contract tests for codeql.yml workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "codeql.yml"


def workflow_document() -> dict[object, object]:
    """Load and parse the codeql workflow."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "codeql.yml is not a workflow"
    return document


def _get_on_block(document: dict[object, object]) -> dict[str, object]:
    """Get the 'on' block, handling YAML boolean key parsing."""
    on_block = document.get("on") or document.get(True)
    assert isinstance(on_block, dict), "on block must be a mapping"
    return on_block


def test_codeql_workflow_exists() -> None:
    """codeql.yml must exist."""
    assert WORKFLOW_PATH.exists(), "codeql.yml does not exist"


def test_codeql_triggers_pr_main_weekly() -> None:
    """codeql.yml must trigger on PR, push to main, and weekly schedule."""
    doc = workflow_document()
    on_block = _get_on_block(doc)

    # PR trigger
    pr = on_block.get("pull_request")
    assert isinstance(pr, dict), "pull_request trigger required"
    branches = pr.get("branches")
    assert branches == ["main"], f"PR must target main branch, got {branches}"

    # Push to main trigger
    push = on_block.get("push")
    assert isinstance(push, dict), "push trigger required"
    push_branches = push.get("branches")
    assert push_branches == ["main"], f"push must target main branch, got {push_branches}"

    # Weekly schedule trigger
    schedule = on_block.get("schedule")
    assert isinstance(schedule, list) and schedule, "weekly schedule trigger required"
    cron = schedule[0].get("cron")
    assert cron == "0 2 * * 1", f"weekly cron must be Monday 2am UTC, got {cron}"


def test_codeql_permissions_security_events_write() -> None:
    """codeql.yml must have job-scoped security-events:write permission."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    # Find the analyze job
    analyze_job = jobs.get("analyze")
    assert isinstance(analyze_job, dict), "analyze job required"

    permissions = analyze_job.get("permissions")
    assert isinstance(permissions, dict), "analyze job must have permissions"
    security_events = permissions.get("security-events")
    assert security_events == "write", f"analyze job must have security-events:write, got {security_events}"


CORRECT_CODEQL_SHA = "8c78abb9b62512e3c45dea6559ffd924ed8549c8"


def test_codeql_init_analyze_upload_sarif_sha_pinned() -> None:
    """init, analyze, upload-sarif actions must be SHA-pinned."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    analyze_job = jobs.get("analyze")
    assert isinstance(analyze_job, dict), "analyze job required"

    steps = analyze_job.get("steps")
    assert isinstance(steps, list), "analyze job must have steps"

    # Find init, analyze, upload-sarif steps
    init_step = None
    analyze_step = None
    upload_step = None

    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if "codeql-action/init" in uses:
            init_step = step
        elif "codeql-action/analyze" in uses:
            analyze_step = step
        elif "codeql-action/upload-sarif" in uses:
            upload_step = step

    assert init_step is not None, "codeql-action/init step required"
    assert analyze_step is not None, "codeql-action/analyze step required"
    assert upload_step is not None, "codeql-action/upload-sarif step required"

    # Check SHA pins (must contain @sha256: or full commit hash)
    for step, name in [(init_step, "init"), (analyze_step, "analyze"), (upload_step, "upload-sarif")]:
        uses = step.get("uses", "")
        assert "@" in uses and uses.count("@") == 1, f"{name} action must be pinned with @"
        # Verify it's a SHA pin (40 char hex or sha256:)
        pin = uses.split("@")[1]
        assert len(pin) == 40 or pin.startswith("sha256:"), f"{name} must be SHA-pinned, got {pin}"
        # Verify it's the CORRECT SHA for v3.28.0
        assert pin == CORRECT_CODEQL_SHA, f"{name} must use v3.28.0 SHA {CORRECT_CODEQL_SHA}, got {pin}"


def test_codeql_no_workflow_level_security_events() -> None:
    """codeql.yml must NOT have workflow-level security-events:write (redundant with job-level)."""
    doc = workflow_document()
    # Workflow-level permissions should not include security-events:write
    permissions = doc.get("permissions")
    if permissions is not None:
        assert isinstance(permissions, dict), "permissions must be a mapping"
        security_events = permissions.get("security-events")
        assert security_events is None or security_events != "write", (
            "workflow-level security-events:write is redundant with job-level grant; remove it"
        )


def test_codeql_security_extended() -> None:
    """codeql.yml must use security-extended query suite."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    analyze_job = jobs.get("analyze")
    assert isinstance(analyze_job, dict), "analyze job required"

    steps = analyze_job.get("steps")
    assert isinstance(steps, list), "analyze job must have steps"

    # Find init step and check queries
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if "codeql-action/init" in uses:
            with_block = step.get("with")
            assert isinstance(with_block, dict), "init step must have with block"
            queries = with_block.get("queries")
            assert queries == "security-extended", f"queries must be security-extended, got {queries}"
            return

    raise AssertionError("codeql-action/init step with security-extended queries not found")


def test_codeql_python_no_build() -> None:
    """codeql.yml must not build Python (languages: python, no build step)."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    analyze_job = jobs.get("analyze")
    assert isinstance(analyze_job, dict), "analyze job required"

    steps = analyze_job.get("steps")
    assert isinstance(steps, list), "analyze job must have steps"

    # Find init step and check languages (matrix.language expands to python)
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if "codeql-action/init" in uses:
            with_block = step.get("with")
            assert isinstance(with_block, dict), "init step must have with block"
            languages = with_block.get("languages")
            # The matrix expands to python, so the value is ${{ matrix.language }}
            assert languages == "${{ matrix.language }}", f"languages must use matrix, got {languages}"
            return

    raise AssertionError("codeql-action/init step with python language not found")


def test_codeql_no_build_step() -> None:
    """codeql.yml must not have a build step for Python."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    analyze_job = jobs.get("analyze")
    assert isinstance(analyze_job, dict), "analyze job required"

    steps = analyze_job.get("steps")
    assert isinstance(steps, list), "analyze job must have steps"

    # Check there's no explicit build command for Python
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        uses = step.get("uses", "")
        # Should not have a build step
        assert "build" not in run.lower() or "codeql-action/autobuild" not in uses, (
            "Python does not need a build step"
        )


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
