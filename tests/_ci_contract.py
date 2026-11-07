"""The shared CI-contract surface: paths, constants and config parsing.

Declared support surface (#476): the pre-commit/CI/hook/Makefile contract test
modules import these from here, not from each other's internals. Bodies moved
verbatim from test_ci_enforcement.py; the jscpd fixture config migrated here
when its single consumer (test_makefile_contract.py) outlived the module that
first owned it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"

# The developer Makefile.
MAKEFILE_PATH = SERVICE_ROOT / "Makefile"

WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"

# The config as the workflow must name it.
WORKFLOW_ROOT_CONFIG = CONFIG_PATH.relative_to(SERVICE_ROOT).as_posix()

ENFORCEMENT_COMMAND = f"pre-commit run --all-files --config {WORKFLOW_ROOT_CONFIG}"

# Every job that reports a result the aggregate must depend on. `audit` and
# `secret-scan` are the supply-chain gates added by #623.
REPORTING_JOBS = {"enforcement", "test-fast", "test-browser", "audit", "secret-scan"}

# GitHub's template form for an errexit shell. The required aggregate is a chain of `test` lines, so its exit
# code is its last line's without errexit.
ERREXIT_SHELL = "bash -e {0}"

# Every file the gates read to decide what to enforce. A commit that edits one of them must run the
# enforcement: the config that decides what is checked cannot itself be the one change that skips the check.
ENFORCEMENT_INPUTS = (
    "crawlutron/config/settings.py",
    "tests/_jscpd_gate_helpers.py",
    # The duplication gate's own implementation.
    "scripts/jscpd_gate.py",
    "scripts/module_graph.py",
    # The Makefile is a gate entry point: `make lint` and `make typecheck` run hooks out of the config.
    "Makefile",
    # The gate entry point itself.
    "scripts/gate",
    ".pre-commit-config.yaml",
    ".jscpd.json",
    "pyrightconfig.json",
    "mypy.ini",
    "pytest.ini",
    "pyproject.toml",
    ".github/workflows/ci.yml",
)

# The single trigger pattern every hook shares, held as one YAML anchor (`*service_trigger`) in
# .pre-commit-config.yaml so the copies cannot drift.
ENFORCEMENT_TRIGGER_PATTERN = r"^(docker/browser-crawler/|(?!docker/))"

# The committed gate entry point every hook resolves to, and the shape of the entry that resolves it.
GATE_HELPER = SERVICE_ROOT / "scripts" / "gate"

GATE_HELPER_PATH = "scripts/gate"

GATE_ENTRY_END = "' gate "

# The one-line `bash -c` prologue every entry must open with, pinned byte for byte so the five copies cannot
# drift.
GATE_ENTRY_PROLOGUE = (
    "bash -c 'for d in "
    '"$(git rev-parse --show-toplevel)/docker/browser-crawler" '
    '"$(git rev-parse --show-toplevel)"; '
    f'do [ -x "$d/{GATE_HELPER_PATH}" ] && exec bash "$d/{GATE_HELPER_PATH}" "$@"; '
    f'done; echo "{GATE_HELPER_PATH} not found in either layout" >&2; exit 1\''
)


def _assert_no_config_exclude(config: str) -> None:
    """`exclude: .*` disables every hook -- all files excluded, all hooks Skipped,
    exit 0 -- so the contract runs the real binary against a synthetic config that
    replaces the real hooks with `exit 1`, proving a bypass would vanish.
    """
    document = yaml.safe_load(config)
    assert isinstance(document, dict), "the pre-commit config must be a mapping"
    exclude = document.get("exclude")
    if exclude is None:
        return
    assert isinstance(exclude, str), "the pre-commit config exclude must be a string pattern"
    # `.*` matches every file — total disable. `*.py` matches every Python file — disables every Python-scoped
    # hook.
    if exclude in (".*", "*.py", ".*.py$"):
        raise ValueError(
            f"config exclude={exclude!r} disables every hook; "
            "only safe repo dirs (venv, cache, git) may be excluded"
        )


def _required_needs(workflow: str) -> set[str]:
    required = _required_job(workflow)
    needs = required.get("needs")
    assert isinstance(needs, list) and needs, "the required aggregate must declare needs"
    return {str(job) for job in needs}


def _required_job(workflow: str) -> dict[str, object]:
    """The required aggregate job block, as the record its checks read."""
    required = _jobs(workflow).get("required")
    assert isinstance(required, dict), "CI must declare the required aggregate job"
    return required


def _result_assertion(job: str) -> str:
    return 'test "${{ needs.' + job + '.result }}" = "success"'


def _required_gate_step(workflow: str) -> dict[str, object]:
    for step in _required_steps(workflow):
        if str(step.get("run", "")).strip():
            return step
    raise AssertionError("the required aggregate must run a script")


def _required_steps(workflow: str) -> list[dict[str, object]]:
    """The required aggregate's steps, as the records their checks read."""
    steps = _required_job(workflow).get("steps")
    assert isinstance(steps, list) and steps, "the required aggregate must declare steps"
    return steps


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
    from scripts.executables import BASH, require_executable

    return subprocess.run(
        [require_executable(BASH), "-e", "-c", script], capture_output=True, text=True, check=False
    )


def _assert_no_continue_on_error(block: dict[str, object], label: str) -> None:
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
    required = _required_job(workflow)
    _assert_no_continue_on_error(required, "the required aggregate")
    assert required.get("if") == "${{ always() }}", "the required aggregate must run even when a need fails"
    for name, job in jobs.items():
        _assert_no_continue_on_error(job, name)
        steps = job.get("steps")
        assert isinstance(steps, list)
        for step in steps:
            _assert_no_continue_on_error(step, f"a step in {name}")

    needs = _required_needs(workflow)
    assert "enforcement" in needs, "the required aggregate must depend on enforcement"
    assert REPORTING_JOBS <= needs, (
        f"the required aggregate must depend on every reporting job, missing {REPORTING_JOBS - needs}"
    )
    _required_gate_shell(workflow)
    script = _required_gate_script(workflow)
    for job_name in sorted(needs):
        assert _result_assertion(job_name) in script, (
            f"the required aggregate never asserts needs.{job_name}.result succeeded"
        )

    results = {job: "success" for job in needs}
    assert _run_script(_render_results(script, results)).returncode == 0, "all-green must pass"
    for job_name in sorted(needs):
        for outcome in ("failure", "cancelled", "skipped"):
            broken = dict(results, **{job_name: outcome})
            completed = _run_script(_render_results(script, broken))
            assert completed.returncode != 0, (
                f"needs.{job}.result = {outcome} still passed the required aggregate:\n{completed.stdout}"
            )


def _job_run_steps(workflow: str) -> list[tuple[str, str]]:
    """Every `run:` step in the workflow as `(job, script)` pairs, skipping steps
    that carry no run string -- the shared sweep both run-based contract checks
    iterate, extracted so the loops cannot become jscpd clone-pairs.
    """
    found: list[tuple[str, str]] = []
    for name, job in _jobs(workflow).items():
        steps = job.get("steps")
        assert isinstance(steps, list)
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str):
                found.append((name, run))
    return found


def _hook_blocks(config: str) -> list[tuple[str, dict[str, object]]]:
    """Every hook in the config as `(id, hook)` pairs, read like pre-commit reads
    them (YAML, not a raw-text regex) -- a quoted id was once invisible here,
    reporting one jscpd gate where the file declared two. Formatting hides it not.
    """
    document = yaml.safe_load(config)
    assert isinstance(document, dict), "the pre-commit config must be a mapping"
    hooks: list[tuple[str, dict[str, object]]] = []
    for repo in document.get("repos") or []:
        assert isinstance(repo, dict), "each repos entry must be a mapping"
        for hook in repo.get("hooks") or []:
            assert isinstance(hook, dict), "each hook must be a mapping"
            hook_id = hook.get("id")
            assert isinstance(hook_id, str) and hook_id, f"hook is missing a string id: {hook!r}"
            hooks.append((hook_id, hook))
    return hooks


def _field(block: dict[str, object], name: str) -> str:
    assert name in block, f"hook is missing {name}"
    return str(block[name])


def _entry(block: dict[str, object]) -> str:
    """The command pre-commit executes, with folded scalars joined. The old `>-`
    assertion was a proxy for the real rule: pre-commit runs the entry as ONE
    command, so a literal block would smuggle newlines in.
    """
    entry = block.get("entry")
    assert isinstance(entry, str) and entry.strip(), "hook is missing an entry"
    assert "\n" not in entry, "entry must be a folded scalar, not a literal one"
    return entry


def _require_exactly_one_jscpd_gate(config: str) -> tuple[str, dict[str, object]]:
    """One duplication gate, by id, whatever the file looks like."""
    hooks = _hook_blocks(config)
    jscpd_hooks = [(hook_id, hook) for hook_id, hook in hooks if hook_id.split("-")[0] == "jscpd"]
    found = [hook_id for hook_id, _ in jscpd_hooks]
    assert len(jscpd_hooks) == 1, f"exactly one jscpd gate is required, found {found}"
    jscpd_id, jscpd = jscpd_hooks[0]
    assert jscpd_id == "jscpd", f"the only jscpd gate must be the whole-service one, found {jscpd_id!r}"
    return jscpd_id, jscpd


def _jobs(workflow: str) -> dict[str, dict[str, object]]:
    document = yaml.safe_load(workflow)
    assert isinstance(document, dict), "the workflow must be a mapping"
    jobs = document.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow must declare jobs"
    typed: dict[str, dict[str, object]] = {}
    for name, job in jobs.items():
        assert isinstance(job, dict), f"job {name} is not a mapping"
        typed[str(name)] = job
    return typed


# A syntactically valid pre-commit config carrying a second, QUOTED jscpd hook
# that would pass a lax threshold; only the quotes tell it from the real config.
_FIXTURE_FILES = r"^(docker/browser-crawler/)?.*\.py$"


def _fixture_hook(hook_id: str, name: str, entry: str, *, quoted: bool = False) -> str:
    """One hook block of the fixture config. The hooks differ only in id, name,
    entry and whether the id is quoted -- the whole defect the fixture exists to
    express -- so the four shared lines are written once.
    """
    rendered_id = f'"{hook_id}"' if quoted else hook_id
    return (
        f"      - id: {rendered_id}\n"
        f"        name: {name}\n"
        f"        entry: {entry}\n"
        "        language: system\n"
        f"        files: {_FIXTURE_FILES}\n"
        "        pass_filenames: false\n"
        "        stages: [pre-commit]\n"
    )


QUOTED_SECOND_JSCPD_HOOK = (
    "repos:\n"
    "  - repo: local\n"
    "    hooks:\n"
    + _fixture_hook("jscpd", "jscpd", ".venv/bin/python -m scripts.jscpd_gate all")
    + _fixture_hook(
        "jscpd-legacy",
        "jscpd legacy",
        "jscpd --threshold 40 app tests || true",
        quoted=True,
    )
)


#: Compile-time, runtime and test docstrings may run no longer than three lines.
_HOOK_MODULE = re.compile(r"\.venv/bin/python -m ([\w.]+)")
