"""Prepare action tests: one gate environment, defined once."""

from __future__ import annotations

from tests._ci_contract import PREPARE_ACTION_PATH, REPORTING_JOBS, WORKFLOW_PATH, _job_run_steps, job_steps


def _cache_step_paths(action: str) -> str:
    """The ``path:`` block of the action's cache step, as one string.

    Scoped to that block so a comment elsewhere explaining what must NOT be cached
    cannot satisfy (or defeat) the check.
    """
    lines = action.splitlines()
    start = next((i for i, line in enumerate(lines) if "uses: actions/cache@" in line), None)
    assert start is not None, "no cache step"
    path_indent = None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if path_indent is None:
            if line.strip().startswith("path:"):
                path_indent = indent
                body.append(line.strip())
            continue
        if indent <= path_indent:
            break
        body.append(line.strip())
    return "\n".join(body)


def test_the_prepare_action_installs_the_frozen_lock() -> None:
    """One gate environment, defined once."""
    action = PREPARE_ACTION_PATH.read_text(encoding="utf-8")
    assert 'python-version: "3.14"' in action, "the prepare action must pin the 3.14 interpreter"
    assert "uv sync --frozen --extra dev" in action, (
        "the prepare action must install from the frozen lock: a bare install "
        "re-resolves the graph CI never froze"
    )
    assert "pip install" not in action, "the prepare action installs with uv, not pip"
    assert "actions/cache@" in action, "the prepare action must cache gate state"
    assert "actions/checkout" not in action, (
        "checkout belongs in the job: GitHub resolves a local action before any step "
        "runs, so an action that checks out runs before the repo is on disk"
    )
    # Per-run pidfiles must never ride a shared cache: every gate job uses this
    # cache key, so a pid from one runner read on another is a stranger's process.
    cached = _cache_step_paths(action)
    assert cached, "the prepare action must declare a cache step with path inputs"
    assert ".gate_fanout" not in cached, (
        "the gate-fanout status dir holds per-run pidfiles and must not be cached"
    )
    for cache_dir in (".mypy_cache", ".ruff_cache"):
        assert cache_dir in cached, f"the prepare action must cache {cache_dir}"


def test_every_gate_job_starts_from_the_prepare_action() -> None:
    """The one environment is the one every gate uses: a job that repeats the
    install steps by hand is a second copy that can drift.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for name in REPORTING_JOBS:
        if name in {"audit", "secret-scan"}:
            continue
        steps = job_steps(workflow, name)
        assert any(str(step.get("uses", "")).startswith("$/.github/actions/prepare") for step in steps), (
            f"job {name} must start from $/.github/actions/prepare: the self-repository "
            "form cannot resolve to another commit (zizmor self-repository)"
        )
        assert any(str(step.get("uses", "")).split("@")[0] == "actions/checkout" for step in steps), (
            f"job {name} must check out before using the local prepare action"
        )
    for name, run in _job_run_steps(workflow):
        assert "pip install" not in run, (
            f"job {name} installs with pip: the lock freezes the graph and pip re-resolves it"
        )
