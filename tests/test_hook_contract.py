"""Every hook shares one trigger and resolves the root in both layouts."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from scripts.executables import GIT, require_executable
from tests.test_ci_enforcement import _jobs
from tests.test_precommit_ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_INPUTS,
    ENFORCEMENT_TRIGGER_PATTERN,
    GATE_ENTRY_END,
    GATE_ENTRY_PROLOGUE,
    GATE_HELPER,
    GATE_HELPER_PATH,
    WORKFLOW_PATH,
    WORKFLOW_ROOT_CONFIG,
    _entry,
    _field,
    _hook_blocks,
)


def test_all_hooks_share_one_trigger() -> None:
    """The five ``files:`` values are one YAML anchor, so they cannot drift apart.

    The trigger was copied five times and never compared -- a ``.gitignore`` edit
    slipped through with every hook Skipped; they are now ``&service_trigger``."""
    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    triggers = {_field(block, "files") for _, block in hooks}
    assert triggers == {ENFORCEMENT_TRIGGER_PATTERN}, (
        f"hooks must all share the one trigger {ENFORCEMENT_TRIGGER_PATTERN!r}; got {sorted(triggers)}"
    )


def test_a_hook_with_a_divergent_trigger_is_caught() -> None:
    """Bites: a hook with a different ``files:`` value fails the all-share-one check.

    Without the chokepoint, adding a sixth hook -- or reverting one copy to a
    per-file pattern -- silently reintroduces the drift."""
    config = CONFIG_PATH.read_text(encoding="utf-8")
    reverted = config.replace("*service_trigger", r"^(docker/browser-crawler/)?.*\.py$")
    reverted_hooks = _hook_blocks(reverted)
    assert _field(reverted_hooks[0][1], "files") == ENFORCEMENT_TRIGGER_PATTERN
    assert _field(reverted_hooks[-1][1], "files") == r"^(docker/browser-crawler/)?.*\.py$"


def test_every_gate_fires_on_every_enforcement_input() -> None:
    """No gate's own configuration can be edited without that gate running.

    A ``files:`` pattern nothing matches is a config nothing checks; ``.jscpd.json``
    was the live instance -- a commit that hid a clone ran no hook at all."""
    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    assert hooks
    for hook_id, block in hooks:
        pattern = _field(block, "files")
        for name in ENFORCEMENT_INPUTS:
            # Both layouts: nested under the parent repo, and at the repository root once the service is
            # extracted.
            for candidate in (f"docker/browser-crawler/{name}", name):
                assert re.search(pattern, candidate), f"hook {hook_id} does not fire on {candidate}"


def test_the_workflow_is_written_for_the_repository_root_it_will_run_from() -> None:
    """GitHub only loads workflows from a repository root.

    Nested today, it has never run; it is written for the root layout it is
    going to, where a ``docker/browser-crawler/`` prefix cannot survive the copy."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "docker/browser-crawler" not in workflow, (
        "the workflow names a path that will not exist once the service is its own repository"
    )
    assert f"--config {WORKFLOW_ROOT_CONFIG}" in workflow
    for name, job in _jobs(workflow).items():
        run_defaults = (job.get("defaults") or {}).get("run") or {}
        assert "working-directory" not in run_defaults, (
            f"job {name} runs in {run_defaults['working-directory']}, which is not the repository root"
        )


def test_every_job_checks_out_full_history() -> None:
    """Every job needs ``fetch-depth: 0`` for the git-history budget ratchet.

    GitHub's default shallow checkout makes the ratchet test SKIP -- a gate
    silently inert in CI; each checkout must fetch the full history."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for name, job in _jobs(workflow).items():
        checkouts = [
            step
            for step in job.get("steps") or []
            if str(step.get("uses", "")).split("@")[0] == "actions/checkout"
        ]
        if not checkouts:
            continue
        for step in checkouts:
            fetch_depth = (step.get("with") or {}).get("fetch-depth")
            assert fetch_depth == 0, (
                f"job {name} checkouts with fetch-depth={fetch_depth!r}; "
                "the metrics-budget history ratchet needs the full history"
            )


def test_every_hook_resolves_the_service_root_in_both_layouts(tmp_path_factory) -> None:
    """The config travels, so its commands must work at either root.

    The dual-layout resolution is one committed helper (``scripts/gate``); the
    property is asserted by EXECUTION in each synthetic layout."""
    config = CONFIG_PATH.read_text(encoding="utf-8")
    for hook_id, block in _hook_blocks(config):
        entry = _entry(block)
        assert GATE_HELPER_PATH in entry, f"hook {hook_id} does not invoke the committed gate helper"
        prologue, command = _split_gate_entry(entry)
        assert command, f"hook {hook_id} resolves the gate helper but names no command to run"
        assert "docker/browser-crawler" in prologue, (
            f"hook {hook_id} does not try the nested service path, so it cannot run "
            "at the repository root of the extracted service"
        )

    for layout in ("nested", "root"):
        service_root, completed = _run_gate_entry_in_layout(tmp_path_factory.mktemp(layout), layout)
        assert completed.returncode == 0, (
            f"the gate entry failed in the {layout} layout, so this config does not work "
            f"in that layout:\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        assert completed.stdout.strip() == str(service_root), (
            f"the gate ran somewhere other than the {layout} service root: "
            f"expected {service_root}, got {completed.stdout.strip()!r}"
        )


def test_every_hook_shares_one_gate_entry_preamble() -> None:
    """The five entry preambles are byte-identical, so they cannot drift.

    YAML has no interpolation, so the one-line lookup cannot be an anchor -- but
    it CAN be checked: editing one preamble and not the others fails here."""
    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    prologues = {_split_gate_entry(_entry(block))[0] for _, block in hooks}
    commands = {_split_gate_entry(_entry(block))[1] for _, block in hooks}
    assert prologues == {GATE_ENTRY_PROLOGUE}, (
        f"the gate entry preambles drifted from the one the contract pins: {sorted(prologues)}"
    )
    assert len(commands) == len(hooks), (
        f"two hooks run the same gate command {sorted(commands)}; each gate must run "
        "something different or it is a copy that can drift from the one it replaced"
    )


def _split_gate_entry(entry: str) -> tuple[str, str]:
    """The entry as ``(bash -c prologue, command)``.

    pre-commit shlex-splits to ``bash -c SCRIPT gate ARGS...``; the split point
    is the prologue script's closing quote before the gate's own ``$0``."""
    prologue, separator, command = entry.partition(GATE_ENTRY_END)
    assert separator, f"gate entry has no {GATE_ENTRY_END!r} terminator: {entry!r}"
    return prologue + "'", command.strip()


def _run_gate_entry_in_layout(repo: Path, layout: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Run a real hook entry against a synthetic repo laid out the named way.

    ``nested`` builds ``<repo>/docker/browser-crawler``; ``root`` builds the
    service at the repo root. Real files, not re-typed stand-ins."""
    # `browser-crawler` here is the pre-extraction layout the hook's prologue still probes for, not this
    # project's name; the nested branch exists to prove the helper resolves there too.
    service_root = repo / "docker" / "browser-crawler" if layout == "nested" else repo
    subprocess.run([require_executable(GIT), "init", "-q", str(repo)], check=True, capture_output=True)
    (service_root / "scripts").mkdir(parents=True)
    (service_root / "scripts" / "gate").write_text(GATE_HELPER.read_text(encoding="utf-8"), encoding="utf-8")
    (service_root / "scripts" / "gate").chmod(0o755)
    # The helper execs the command it is handed; the synthetic service has no venv of its own, so the command
    # names the stub interpreter directly.
    (service_root / ".venv" / "bin").mkdir(parents=True)
    interpreter = service_root / ".venv" / "bin" / "python"
    interpreter.write_text('#!/bin/sh\nexec python3 "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    subprocess.run([require_executable(GIT), "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    # pre-commit runs an entry as `shlex.split(entry)` with the repository root as the working directory, so
    # the first four tokens -- program, -c, the dual-layout script, and the gate's own $0 name -- are exactly
    prologue = shlex.split(_entry(_hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))[0][1]))[:4]
    assert prologue[:2] == ["bash", "-c"], f"unexpected entry shape: {prologue!r}"
    completed = subprocess.run(
        [*prologue, ".venv/bin/python", "-c", "import os; print(os.getcwd())"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return service_root.resolve(), completed
