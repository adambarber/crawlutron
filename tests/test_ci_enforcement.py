"""CI runs every pre-commit hook through one surface, without reruns."""

from __future__ import annotations

import re
import subprocess

import pytest
import yaml

from scripts import jscpd_gate as gate
from scripts.executables import BASH, require_executable
from tests.test_precommit_ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_COMMAND,
    ENFORCEMENT_TRIGGER_PATTERN,
    ERREXIT_SHELL,
    REPORTING_JOBS,
    SERVICE_ROOT,
    WORKFLOW_PATH,
    _assert_no_config_exclude,
    _entry,
    _field,
    _hook_blocks,
    _require_exactly_one_jscpd_gate,
)


def _required_needs(workflow: str) -> set[str]:
    required = _jobs(workflow).get("required")
    assert isinstance(required, dict), "CI must declare the required aggregate job"
    needs = required.get("needs")
    assert isinstance(needs, list) and needs, "the required aggregate must declare needs"
    return {str(job) for job in needs}


def _jobs(workflow: str) -> dict:
    document = yaml.safe_load(workflow)
    assert isinstance(document, dict), "the workflow must be a mapping"
    jobs = document.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow must declare jobs"
    return jobs


def _result_assertion(job: str) -> str:
    return 'test "${{ needs.' + job + '.result }}" = "success"'


def _required_gate_step(workflow: str) -> dict:
    for step in _jobs(workflow)["required"].get("steps") or []:
        if str(step.get("run", "")).strip():
            return step
    raise AssertionError("the required aggregate must run a script")


def _required_gate_script(workflow: str) -> str:
    """The shell the required aggregate actually runs, expressions intact."""
    return str(_required_gate_step(workflow)["run"])


def _required_gate_shell(workflow: str) -> str:
    """The aggregate's shell, which the gate depends on and so must be declared: the
    script is a chain of `test` lines, and without errexit its exit code is the LAST
    line's, so a mid-chain failure reports success by accident.
    """
    shell = _required_gate_step(workflow).get("shell")
    assert shell == ERREXIT_SHELL, (
        f"the required aggregate must declare shell {ERREXIT_SHELL!r}, found {shell!r}: "
        "without errexit its exit code is the last test line's, not the failing one"
    )
    return str(shell)


def _render_results(script: str, results: dict[str, str]) -> str:
    for job, result in results.items():
        script = script.replace("${{ needs." + job + ".result }}", result)
    assert "${{" not in script, f"unrendered expression left in the required script:\n{script}"
    return script


def _run_script(script: str) -> subprocess.CompletedProcess[str]:
    """Execute as GitHub executes it: `bash -e {0}`."""
    return subprocess.run(
        [require_executable(BASH), "-e", "-c", script], capture_output=True, text=True, check=False
    )


def _assert_no_continue_on_error(block: dict, label: str) -> None:
    """Reject any `continue-on-error` GitHub could evaluate true: `${{ true }}` loads
    as a STRING, so an `is not True` check passes the value GitHub runs as true --
    the only safe value is absent or the literal scalar `false`.
    """
    value = block.get("continue-on-error")
    assert value is False or value is None, (
        f"{label} is continue-on-error={value!r}, so needs.result is hollow; "
        "it must be false or absent (expressions are evaluated by GitHub)"
    )


def _assert_failure_propagation(workflow: str) -> None:
    """A required check that cannot fail is not a gate: `continue-on-error: true`
    (which GitHub reports as success) and deleting the result assertion both slipped
    the old contract -- the refusal is checked by running the aggregate's script.
    """
    jobs = _jobs(workflow)
    required = jobs["required"]
    _assert_no_continue_on_error(required, "the required aggregate")
    assert required.get("if") == "${{ always() }}", "the required aggregate must run even when a need fails"
    for name, job in jobs.items():
        _assert_no_continue_on_error(job, name)
        for step in job.get("steps") or []:
            _assert_no_continue_on_error(step, f"a step in {name}")

    needs = _required_needs(workflow)
    assert "enforcement" in needs, "the required aggregate must depend on enforcement"
    assert REPORTING_JOBS <= needs, (
        f"the required aggregate must depend on every reporting job, missing {REPORTING_JOBS - needs}"
    )
    _required_gate_shell(workflow)
    script = _required_gate_script(workflow)
    for job in sorted(needs):
        assert _result_assertion(job) in script, (
            f"the required aggregate never asserts needs.{job}.result succeeded"
        )

    results = {job: "success" for job in needs}
    assert _run_script(_render_results(script, results)).returncode == 0, "all-green must pass"
    for job in sorted(needs):
        for outcome in ("failure", "cancelled", "skipped"):
            broken = dict(results, **{job: outcome})
            completed = _run_script(_render_results(script, broken))
            assert completed.returncode != 0, (
                f"needs.{job}.result = {outcome} still passed the required aggregate:\n{completed.stdout}"
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

    assert all("pre-push" not in (block.get("stages") or []) for _, block in hooks)
    assert all(block.get("stages") == ["pre-commit"] for _, block in hooks)

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
    assert "pre-commit==4.6.2" in (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
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
        "tests/test_pyright_budget.py",
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
        for step in job.get("steps") or []:
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for tool in hook_tools:
                assert tool not in run, (
                    f"job {name} re-runs a pre-commit hook command ({tool!r}): "
                    "enforcement already runs it; a duplicate is a second copy "
                    "that can drift from the gate"
                )
