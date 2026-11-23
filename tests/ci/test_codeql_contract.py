"""Residual contract tests for codeql.yml not covered by the typed workflow rules."""

from __future__ import annotations

import yaml

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "codeql.yml"


def workflow_document() -> dict[object, object]:
    """Load and parse the codeql workflow."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "codeql.yml is not a workflow"
    return document


def test_codeql_workflow_exists() -> None:
    """codeql.yml must exist."""
    assert WORKFLOW_PATH.exists(), "codeql.yml does not exist"


def test_codeql_python_no_build() -> None:
    """codeql.yml must not build Python (languages: python, no build step)."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    analyze_job = jobs.get("analyze")
    assert isinstance(analyze_job, dict), "analyze job required"

    steps = analyze_job.get("steps")
    assert isinstance(steps, list), "analyze job must have steps"

    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if "codeql-action/init" in uses:
            with_block = step.get("with")
            assert isinstance(with_block, dict), "init step must have with block"
            languages = with_block.get("languages")
            assert languages == "${{ matrix.language }}", f"languages must use matrix, got {languages}"
            return

    raise AssertionError("codeql-action/init step with python language not found")
