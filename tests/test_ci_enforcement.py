"""CI runs every pre-commit hook through one surface, without reruns."""

from __future__ import annotations

import re

import pytest

from scripts import jscpd_gate as gate
from tests._ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_COMMAND,
    ENFORCEMENT_TRIGGER_PATTERN,
    REPORTING_JOBS,
    WORKFLOW_PATH,
    _assert_failure_propagation,
    _assert_no_config_exclude,
    _entry,
    _field,
    _hook_blocks,
    _jobs,
    _require_exactly_one_jscpd_gate,
    _required_needs,
)


def test_ci_runs_every_precommit_hook_through_one_surface() -> None:
    config = CONFIG_PATH.read_text(encoding="utf-8")
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    hooks = _hook_blocks(config)
    assert hooks
    _assert_no_config_exclude(config)

    jscpd_id, jscpd = _require_exactly_one_jscpd_gate(config)
    assert jscpd_id == "jscpd"
    assert "jscpd-app" not in config
    assert "jscpd-tests" not in config
    assert not [hook_id for hook_id, _ in hooks if hook_id in {"jscpd-app", "jscpd-tests"}]

    # #557: coverage runs at pre-push (per-commit measurement trains
    # --no-verify) and stays out of CI's enforcement surface.
    assert all(block.get("stages") in (["pre-commit"], ["commit-msg"], ["pre-push"]) for _, block in hooks)
    coverage_hooks = [(hook_id, block) for hook_id, block in hooks if hook_id == "coverage"]
    assert len(coverage_hooks) == 1, "exactly one coverage surface hook"
    assert coverage_hooks[0][1].get("stages") == ["pre-push"], (
        "the coverage hook must run at pre-push, not per-commit"
    )
    assert "--cov-fail-under" not in config, (
        "coverage is measured-descriptive; a threshold is an owner ruling, not a default"
    )

    entry = _entry(jscpd)
    assert ".venv/bin/python -m scripts.jscpd_gate all" in entry
    # The floor is on the command line rather than inside the gate so the command stays the one place a
    # reviewer looks to see what duplication costs
    floor = re.search(r"--min-lines (\d+) --min-tokens (\d+) --threshold (\S+)", entry)
    assert floor is not None, f"the entry states no single floor triple: {entry!r}"
    assert floor.group(1) == str(gate.configured_floor()[0])
    assert floor.group(2) == str(gate.configured_floor()[1])
    assert float(floor.group(3)) == gate.configured_floor()[2]
    assert "--app-min-lines" not in entry, (
        "the per-tree floor split is gone; one number must serve both trees"
    )
    assert _field(jscpd, "files") == ENFORCEMENT_TRIGGER_PATTERN

    assert workflow.count(ENFORCEMENT_COMMAND) == 1
    assert "scripts/jscpd_gate.py" not in workflow
    required = _required_needs(workflow)
    assert required == REPORTING_JOBS
    # The enforcement job builds its venv from ``-e .[dev]`` alone; re-installing
    # a specific tool on top would reintroduce a second, drifting declaration.
    assert ".[dev]" in workflow
    _assert_failure_propagation(workflow)


def test_no_ci_job_reruns_a_precommit_hook_command() -> None:
    """CI must not run a gate twice in one run: `lint`/`typecheck` used to re-run
    what `enforcement`'s hooks already execute -- three gates twice, nothing
    comparing the copies. Any `run:` must not repeat a tool a hook runs.
    """
    hook_tools = {
        "python -m ruff check",
        "python -m mypy",
        "python -m scripts.jscpd_gate",
        "basedpyright",
        "tests/invariants/",
    }
    assert_no_hook_command_rerun(WORKFLOW_PATH.read_text(encoding="utf-8"), hook_tools)


def test_a_ci_job_that_reruns_a_hook_command_is_caught() -> None:
    """Bites: adding a job that re-runs a hook command fails the check."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    broken = workflow.replace(
        "- name: Run all configured hooks\n        run: pre-commit run --all-files --config "
        ".pre-commit-config.yaml",
        "- name: Run all configured hooks\n        run: pre-commit run --all-files --config "
        ".pre-commit-config.yaml\n      - name: Drift copy\n        run: python -m ruff check app tests "
        "scripts",
    )
    with pytest.raises(AssertionError):
        assert_no_hook_command_rerun(broken, {"python -m ruff check"})


def assert_no_hook_command_rerun(workflow: str, hook_tools) -> None:
    """No CI job re-runs a command the enforcement job already runs: the duplicate and
    the gate are two copies of one check, only the edited one drifts -- the error names
    the job so the step is findable from the failure alone.
    """
    for name, job in _jobs(workflow).items():
        steps = job.get("steps")
        assert isinstance(steps, list)
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for tool in hook_tools:
                assert tool not in run, (
                    f"job {name} re-runs a pre-commit hook command ({tool!r}): "
                    "enforcement already runs it; a duplicate is a second copy "
                    "that can drift from the gate"
                )
