"""CI runs every pre-commit hook through one surface, without reruns."""

from __future__ import annotations

import re

import pytest
import yaml

from scripts import jscpd_gate as gate
from tests._ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_COMMAND,
    ENFORCEMENT_TRIGGER_PATTERN,
    REPORTING_JOBS,
    SERVICE_ROOT,
    WORKFLOW_PATH,
    _assert_failure_propagation,
    _assert_no_config_exclude,
    _assert_no_continue_on_error,
    _entry,
    _field,
    _hook_blocks,
    _job_run_steps,
    _jobs,
    _require_exactly_one_jscpd_gate,
    _required_needs,
)

# The version-triggered GHCR publish workflow (#949), contracted alongside ci.yml.
RELEASE_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "release.yml"


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
    _assert_frozen_lock_install(workflow)
    required = _required_needs(workflow)
    assert required == REPORTING_JOBS
    # The enforcement job builds its venv from the frozen lock's dev extra;
    # re-installing a specific tool on top reintroduces a drifting declaration.
    assert "--extra dev" in workflow
    assert "pip install" not in workflow
    _assert_failure_propagation(workflow)


def _assert_frozen_lock_install(workflow: str) -> None:
    """Every job that installs dependencies installs from the frozen lock via uv
    (#622): a bare `pip install -e .[dev]` re-resolves the transitive graph at
    install time, so CI tests against versions the lock never froze.
    """
    runs = _job_run_steps(workflow)
    for name, run in runs:
        assert "pip install" not in run, (
            f"job {name} installs with pip: the lock freezes the graph and "
            "pip re-resolves it; install with `uv sync --frozen --extra dev`"
        )
    for name, run in runs:
        if "uv sync" in run:
            assert "--frozen" in run, f"job {name} syncs without --frozen"
    installs = [run for _, run in runs if "uv sync" in run]
    assert len(installs) >= 2, f"expected the install jobs to sync from the lock, found {len(installs)}"
    assert "uv.lock" in workflow, "CI must key its cache on uv.lock"


def test_ci_runs_a_dependency_audit_and_secret_scan() -> None:
    """#623: supply-chain gates. pip-audit against the frozen lock's export
    catches known CVEs; gitleaks catches committed secrets. Both run as jobs
    the required aggregate depends on, so skipping one is not green.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = _jobs(workflow)
    for job in ("audit", "secret-scan"):
        assert job in jobs, f"CI must declare the {job} job"
        _assert_no_continue_on_error(jobs[job], job)

    required = _required_needs(workflow)
    assert {"audit", "secret-scan"} <= required, (
        "the required aggregate must depend on the audit and secret-scan jobs"
    )

    def job_runs(job: dict[str, object]) -> str:
        steps = job.get("steps")
        assert isinstance(steps, list)
        return " ".join(str(step.get("run")) for step in steps if isinstance(step, dict) and "run" in step)

    audit_runs = job_runs(jobs["audit"])
    assert "uv export --frozen" in audit_runs, "audit must export the frozen lock, not live deps"
    assert "pip-audit" in audit_runs, "audit must run pip-audit"

    assert "gitleaks.io" not in workflow, "workflow must not use a mutable gitleaks.io ref"


def test_every_action_is_sha_pinned_and_permissions_are_declared() -> None:
    """#624: mutable tags (`@v4`) are repointable; every `uses:` must pin a
    full-length commit SHA, and the workflow must declare its permissions so
    the default write-all token scope cannot leak into a compromised step.
    """
    for workflow_path in (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH):
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)

        def walk(value: object, file_name: str = workflow_path.name) -> None:
            if isinstance(value, dict):
                uses = value.get("uses")
                if isinstance(uses, str) and "@" in uses:
                    ref = uses.rsplit("@", 1)[1]
                    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                        f"action ref {uses!r} in {file_name} is not a full-length commit SHA"
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(document.get("jobs"))
    ci = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(ci, dict)
    permissions = ci.get("permissions")
    assert permissions == "read-all", (
        f"CI must declare top-level permissions: read-all, found {permissions!r}"
    )


def test_release_workflow_has_write_only_on_packages() -> None:
    """#949: the GHCR publish needs write scope ci.yml's read-all forbids, so the
    release workflow declares it narrowly -- packages write, contents read."""
    assert RELEASE_WORKFLOW_PATH.is_file(), "a version-triggered release workflow must exist"
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    permissions = document.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("packages") == "write"
    assert permissions.get("contents") == "read"
    triggers = document["on"] if isinstance(document.get("on"), dict) else document.get(True)
    assert isinstance(triggers, dict), "the release workflow must declare an 'on' trigger"
    push = triggers.get("push") or {}
    assert "v*" in str(push.get("tags")), "the release workflow must trigger on v* tag pushes"


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
    needle = f"- name: Run all configured hooks\n        run: {ENFORCEMENT_COMMAND}"
    inserted = f"{needle}\n      - name: Drift copy\n        run: python -m ruff check app tests scripts"
    broken = workflow.replace(needle, inserted)
    with pytest.raises(AssertionError):
        assert_no_hook_command_rerun(broken, {"python -m ruff check"})


def assert_no_hook_command_rerun(workflow: str, hook_tools) -> None:
    """No CI job re-runs a command the enforcement job already runs: the duplicate and
    the gate are two copies of one check, only the edited one drifts -- the error names
    the job so the step is findable from the failure alone.
    """
    for name, run in _job_run_steps(workflow):
        for tool in hook_tools:
            assert tool not in run, (
                f"job {name} re-runs a pre-commit hook command ({tool!r}): "
                "enforcement already runs it; a duplicate is a second copy "
                "that can drift from the gate"
            )


def test_browser_job_selects_only_browser_marked_tests() -> None:
    """The browser job exists to run the browser suite: a filter that admits
    anything else re-runs the fast suite (~3,400 tests) on every push."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = _jobs(workflow)["test-browser"].get("steps")
    assert isinstance(steps, list)
    browser_step = str(
        next(
            step.get("run")
            for step in steps
            if isinstance(step, dict) and "Run pytest (browser)" == str(step.get("name"))
        )
    )
    assert browser_step.strip() == '.venv/bin/python -m pytest -m "browser"', browser_step


def test_enforcement_job_caches_gate_state() -> None:
    """Gate-state caches (.mypy_cache/.ruff_cache/.gate_fanout) skip cold-start
    re-work on warm runs; keyed on the lock + pyproject so dependency bumps
    invalidate."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = _jobs(workflow)["enforcement"].get("steps")
    assert isinstance(steps, list)
    caches = [
        step
        for step in steps
        if isinstance(step, dict) and str(step.get("name", "")).startswith("Cache gate state")
    ]
    assert len(caches) == 1, f"exactly one gate-state cache step expected, found {len(caches)}"
    with_block = caches[0].get("with")
    assert isinstance(with_block, dict)
    paths = str(with_block["path"])
    for expected in (".mypy_cache", ".ruff_cache", ".gate_fanout"):
        assert expected in paths, f"gate-state cache missing {expected}"
    key = str(with_block["key"])
    assert "hashFiles('uv.lock', 'pyproject.toml')" in key, key


def test_conftest_leaves_bytecode_writing_enabled() -> None:
    """S4: conftest must not set PYTHONDONTWRITEBYTECODE so modules compile to
    .pyc once and are reused across xdist workers and runs."""
    import os

    import tests.conftest as conftest_module

    _ = conftest_module  # conftest's import side effect applies its env at startup
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") != "1", (
        "conftest disables bytecode writes; remove the PYTHONDONTWRITEBYTECODE=1 line"
    )


def test_ci_precompiles_bytecode_once_in_test_fast() -> None:
    """S4: the test-fast job precompiles .pyc once after install so every worker
    reuses them instead of recompiling ~90k lines per run."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = _jobs(workflow)["test-fast"].get("steps")
    assert isinstance(steps, list)
    runs = " ".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "compileall" in runs, "test-fast must precompile bytecode with compileall"
    # #949: the compile scope derives from the uv member src roots (glob, never a
    # hand-listed pre-src-layout package dir); this pins that derived glob.
    assert "packages/*/src" in runs, "compileall must derive its scope from the member src roots"
    assert "tests" in runs and "scripts" in runs, "compileall must cover the test and script trees"


def test_ci_release_workflow_pins_version_consistency() -> None:
    """#949: the smoke asserts the BAKED version equals the tag it was pushed as,
    so the image cannot carry a version that disagrees with its :{version} tag."""
    runs = [str(run) for _, run in _job_run_steps(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))]
    joined = "\n".join(runs)
    assert "ghcr.io/" in joined, "the release workflow must push to GHCR"
    assert "tr '[:upper:]' '[:lower:]'" in joined, "GHCR tag must be lowercased"
    assert "steps.version.outputs.native" in joined, (
        "the smoke must compare the baked version against the git-describe version"
    )
    # The image's CMD binds loopback by default; Docker -p cannot reach a
    # loopback-bound process, so the smoke must bind 0.0.0.0 (compose precedent).
    assert "-e CRAWLUTRON_HOST=0.0.0.0" in joined, (
        "the smoke must set CRAWLUTRON_HOST=0.0.0.0 for -p to reach the service"
    )


def test_ci_regenerates_the_version_artifact_before_the_suite() -> None:
    """#949: the version artifact is gitignored, so a CI checkout has none until
    regen_version writes it; both gate-carrying jobs must see the git-describe
    version."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for job in ("enforcement", "test-fast"):
        steps = _jobs(workflow)[job].get("steps")
        assert isinstance(steps, list)
        regen = [str(step.get("run", "")) for step in steps if isinstance(step, dict)]
        assert any("scripts/regen_version.py" in run for run in regen), (
            f"job {job} must regenerate the version artifact before running tests"
        )
