"""Contract tests for scorecard.yml workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "scorecard.yml"


def workflow_document() -> dict[object, object]:
    """Load and parse the scorecard workflow."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "scorecard.yml is not a workflow"
    return document


def _get_on_block(document: dict[object, object]) -> dict[str, object]:
    """Get the 'on' block, handling YAML boolean key parsing."""
    on_block = document.get("on") or document.get(True)
    assert isinstance(on_block, dict), "on block must be a mapping"
    return on_block


def _steps(document: dict[object, object]) -> list[object]:
    """Find the scorecard analysis steps."""
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"
    job = jobs.get("analysis")
    assert isinstance(job, dict), "analysis job required"
    steps = job.get("steps")
    assert isinstance(steps, list), "steps must be a list"
    return steps


def test_scorecard_analyzes_pull_request_head() -> None:
    """Scorecard must produce results for PR heads, not just main pushes."""
    on_block = _get_on_block(workflow_document())
    assert "pull_request" in on_block, "scorecard.yml must trigger on pull_request"


def test_scorecard_publishes_only_from_trusted_refs() -> None:
    """PR runs must not publish the badge from untrusted code."""
    for step in _steps(workflow_document()):
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if not isinstance(uses, str) or "scorecard-action" not in uses:
            continue
        publish = (step.get("with") or {}).get("publish_results")
        if publish is None:
            continue
        assert publish == "${{ github.event_name != 'pull_request' }}", (
            f"publish_results must be guarded for PR runs, got {publish}"
        )
        return
    raise AssertionError("scorecard-action step not found")
