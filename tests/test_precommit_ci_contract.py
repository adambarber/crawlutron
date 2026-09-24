"""The pre-commit config is the single CI enforcement surface.

This contract is intentionally parsed as text: the workflow is a small,
stable subset of YAML, and a text check makes the enforcement relationship
visible without making this meta-gate depend on a YAML parser's data model.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"
ENFORCEMENT_COMMAND = "pre-commit run --all-files --config docker/browser-crawler/.pre-commit-config.yaml"
REPORTING_JOBS = {"lint", "typecheck", "test-fast"}


def _hook_ids(config: str) -> set[str]:
    return set(re.findall(r"^\s*- id: ([A-Za-z0-9_-]+)\s*$", config, re.MULTILINE))


def _required_needs(workflow: str) -> set[str]:
    required = re.search(r"^  required:\n(?P<body>.*?)(?=^\S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert required is not None, "CI must declare the required aggregate job"
    needs = re.search(r"^\s+needs: \[(?P<jobs>[^\]]+)\]", required.group("body"), re.MULTILINE)
    assert needs is not None, "the required aggregate must declare needs"
    return {job.strip() for job in needs.group("jobs").split(",")}


def test_ci_runs_every_precommit_hook_through_one_surface() -> None:
    config = CONFIG_PATH.read_text(encoding="utf-8")
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    hook_ids = _hook_ids(config)

    assert hook_ids
    assert workflow.count(ENFORCEMENT_COMMAND) == 1
    assert "tests/_jscpd_gate.py" not in workflow
    assert "stages: [pre-push]" not in config
    assert "stages: [pre-commit, pre-push]" in config

    required = _required_needs(workflow)
    assert "enforcement" in required
    assert required - {"enforcement"} <= REPORTING_JOBS
    assert "pre-commit==4.6.2" in (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
