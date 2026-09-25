"""The pre-commit config is the single CI enforcement surface."""

from __future__ import annotations

import json
import re
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"
EXPECTATIONS_PATH = SERVICE_ROOT / "jscpd-expectations.json"
ENFORCEMENT_COMMAND = "pre-commit run --all-files --config docker/browser-crawler/.pre-commit-config.yaml"
REPORTING_JOBS = {"lint", "typecheck", "test-fast"}


def _hook_blocks(config: str) -> list[tuple[str, str]]:
    starts = list(re.finditer(r"^      - id: ([A-Za-z0-9_-]+)\s*$", config, re.MULTILINE))
    return [
        (match.group(1), config[match.start() : starts[index + 1].start() if index + 1 < len(starts) else len(config)])
        for index, match in enumerate(starts)
    ]


def _field(block: str, name: str) -> str:
    match = re.search(rf"^\s+{re.escape(name)}:\s*(?P<value>.*?)\s*$", block, re.MULTILINE)
    assert match is not None, f"hook is missing {name}"
    return match.group("value")


def _entry(block: str) -> str:
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s+entry:\s*>-\s*$", line):
            values = [line.split(":", 1)[1].strip()]
            for continuation in lines[index + 1 :]:
                if re.match(r"^\s{8}[A-Za-z][A-Za-z0-9_-]*:", continuation):
                    break
                if continuation.strip():
                    values.append(continuation.strip())
            return " ".join(values)
    raise AssertionError("hook is missing a folded entry")


def _required_needs(workflow: str) -> set[str]:
    required = re.search(r"^  required:\n(?P<body>.*?)(?=^\S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert required is not None, "CI must declare the required aggregate job"
    needs = re.search(r"^\s+needs: \[(?P<jobs>[^\]]+)\]", required.group("body"), re.MULTILINE)
    assert needs is not None, "the required aggregate must declare needs"
    return {job.strip() for job in needs.group("jobs").split(",")}


def test_ci_runs_every_precommit_hook_through_one_surface() -> None:
    config = CONFIG_PATH.read_text(encoding="utf-8")
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    hooks = _hook_blocks(config)
    assert hooks

    jscpd_hooks = [hook for hook in hooks if hook[0] == "jscpd" or hook[0].startswith("jscpd-")]
    assert len(jscpd_hooks) == 1
    jscpd_id, jscpd = jscpd_hooks[0]
    assert jscpd_id == "jscpd"
    assert "jscpd-app" not in config
    assert "jscpd-tests" not in config

    assert all("pre-push" not in block for _, block in hooks)
    assert all(_field(block, "stages") == "[pre-commit]" for _, block in hooks)

    entry = _entry(jscpd)
    assert ".venv/bin/python tests/_jscpd_gate.py all" in entry
    assert "--min-lines 8" in entry
    assert "--min-tokens 40" in entry
    assert "--threshold 0" in entry
    assert _field(jscpd, "files") == r"^(docker/browser-crawler/)?((app|tests|scripts)/.*\.py|jscpd-expectations\.json|\.jscpd\.json)$"

    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    assert all("profiles" not in pair for pair in expectations["pairs"])

    assert workflow.count(ENFORCEMENT_COMMAND) == 1
    assert "tests/_jscpd_gate.py" not in workflow
    required = _required_needs(workflow)
    assert "enforcement" in required
    assert required - {"enforcement"} <= REPORTING_JOBS
    assert "pre-commit==4.6.2" in (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
