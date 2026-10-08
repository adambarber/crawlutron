"""The pre-commit config is the single CI enforcement surface."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import jscpd_gate as gate
from scripts.executables import BASH, GIT, require_executable, require_repo_tool

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
# The developer Makefile. It is the one place outside CI that a person runs a
# gate by hand, which makes it the most likely place for a gate to acquire a
# second spelling: CI is held to the config by the tests below, and nothing was
# holding the Makefile to anything.
MAKEFILE_PATH = SERVICE_ROOT / "Makefile"
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"
EXPECTATIONS_PATH = SERVICE_ROOT / "jscpd-expectations.json"
# The config as the workflow must name it. The workflow is a repository-root
# `.github/workflows/ci.yml` and is written for the layout where this service IS
# the repository, so the config it names is this service's own file at the root
# of that repository. Deriving the expected string from SERVICE_ROOT keeps this
# test correct in both layouts: the workflow and this file are copied verbatim on
# extraction, and the one thing that must never reappear is a
# `docker/browser-crawler/` prefix, which stops naming anything the moment the
# service becomes its own repository. `relative_to` is not a no-op assertion: if
# the config ever moved off the service root, this would say so.
WORKFLOW_ROOT_CONFIG = CONFIG_PATH.relative_to(SERVICE_ROOT).as_posix()
ENFORCEMENT_COMMAND = f"pre-commit run --all-files --config {WORKFLOW_ROOT_CONFIG}"
# Every job that reports a result the aggregate must depend on. test-browser is
# here because it is no longer `if: workflow_dispatch`: while it carried that
# condition GitHub reported a *skipped* job as a success, so the aggregate saw a
# green it had not earned and the browser suite could never fail a build.
REPORTING_JOBS = {"enforcement", "test-fast", "test-browser"}
# GitHub's template form for an errexit shell. The required aggregate is a chain
# of `test` lines, so its exit code is its last line's without errexit.
ERREXIT_SHELL = "bash -e {0}"
# Every file the gates read to decide what to enforce. A commit that edits one of
# them must run the enforcement: the config that decides what is checked cannot
# itself be the one change that skips the check.
ENFORCEMENT_INPUTS = (
    "app/config/settings.py",
    "tests/test_jscpd_gate.py",
    # The duplication gate's own implementation. A commit that edits it changes
    # what every root is measured against, so it belongs beside the config that
    # names the floor -- the same "the thing that decides cannot itself be the
    # change that skips the check" case `scripts/gate` is named for below.
    "scripts/jscpd_gate.py",
    "scripts/module_graph.py",
    # The Makefile is a gate entry point: `make lint` and `make typecheck` run
    # hooks out of the config. A commit that edits it can silently retarget or
    # repoint a gate, so it belongs in the set of edits that must re-run
    # enforcement. It was absent while the file still declared its own ruff and
    # mypy commands, which is how those copies survived the contract tests.
    "Makefile",
    # The gate entry point itself. Every hook resolves to this file, so a commit
    # that edits it changes what all five gates actually run -- exactly the
    # "the config that decides what is checked cannot itself be the one change
    # that skips the check" case, and the reason it is named here rather than
    # left to the trigger to notice.
    "scripts/gate",
    ".pre-commit-config.yaml",
    ".jscpd.json",
    "jscpd-expectations.json",
    "pyrightconfig.json",
    "mypy.ini",
    "pytest.ini",
    "pyproject.toml",
    "requirements-dev.txt",
    ".github/workflows/ci.yml",
)
# The single trigger pattern every hook shares, held as one YAML anchor
# (`*service_trigger`) in .pre-commit-config.yaml so the copies cannot drift.
# It matches any file that belongs to this service in either layout — nested
# under the parent repo as docker/browser-crawler/*, or at the repository root
# once extracted — and excludes the parent repo's other docker services.
ENFORCEMENT_TRIGGER_PATTERN = r"^(docker/browser-crawler/|(?!docker/))"
# The committed gate entry point every hook resolves to, and the shape of the
# entry that resolves it. See scripts/gate for why the lookup cannot move into
# the helper: the helper sits at two different relative paths in the two layouts
# and pre-commit always runs hooks from the repository root.
GATE_HELPER = SERVICE_ROOT / "scripts" / "gate"
GATE_HELPER_PATH = "scripts/gate"
GATE_ENTRY_END = "' gate "
# The one-line `bash -c` prologue every entry must open with, pinned byte for
# byte so the five copies cannot drift. YAML has no string interpolation, so
# this cannot be an anchor the way `*service_trigger` is; checking it is the
# substitute.
GATE_ENTRY_PROLOGUE = (
    "bash -c 'for d in "
    '"$(git rev-parse --show-toplevel)/docker/browser-crawler" '
    '"$(git rev-parse --show-toplevel)"; '
    f'do [ -x "$d/{GATE_HELPER_PATH}" ] && exec bash "$d/{GATE_HELPER_PATH}" "$@"; '
    f'done; echo "{GATE_HELPER_PATH} not found in either layout" >&2; exit 1\''
)
# A syntactically valid config carrying a second, quoted jscpd hook that would
# pass a lax threshold. Nothing distinguishes it from the real config to a
# reader of the raw text except the quotes.
# This fixture's own `files` pattern, which is deliberately NOT
# ENFORCEMENT_TRIGGER_PATTERN: the point is a second hook that would scan the
# same files, and borrowing the shared anchor here would make the fixture depend
# on a constant it is meant to be a hand-written example of.
_FIXTURE_FILES = r"^(docker/browser-crawler/)?.*\.py$"


def _fixture_hook(hook_id: str, name: str, entry: str, *, quoted: bool = False) -> str:
    """One hook block of the fixture config.

    The two hooks below differ only in id, name, entry, and whether the id is
    quoted -- which is the whole defect the fixture exists to express -- so the
    four lines they share are written once instead of twice.
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


def _hook_blocks(config: str) -> list[tuple[str, dict]]:
    """Every hook in the config, as `(id, hook)` pairs read by a YAML parser.

    This used to be a regex over the raw text anchored to exactly six spaces of
    indentation, which meant a hook id written `- id: "jscpd-legacy"` was
    invisible here: the contract could report one jscpd gate while the file
    declared two. pre-commit reads ids as YAML values, so this test reads them
    the same way. Quoting, indentation and key order are no longer ways to be
    invisible.
    """
    document = yaml.safe_load(config)
    assert isinstance(document, dict), "the pre-commit config must be a mapping"
    hooks: list[tuple[str, dict]] = []
    for repo in document.get("repos") or []:
        assert isinstance(repo, dict), "each repos entry must be a mapping"
        for hook in repo.get("hooks") or []:
            assert isinstance(hook, dict), "each hook must be a mapping"
            hook_id = hook.get("id")
            assert isinstance(hook_id, str) and hook_id, f"hook is missing a string id: {hook!r}"
            hooks.append((hook_id, hook))
    return hooks


PRE_COMMIT = "pre-commit"


def _run_precommit_on_temp_config(temp_dir: Path, config_text: str) -> subprocess.CompletedProcess[str]:
    """Run the REAL pre-commit binary against a synthetic config in a temp dir.

    Returns the CompletedProcess; the caller asserts on returncode. This is how
    the gate validates that no `exclude` pattern disables every hook — because
    it runs pre-commit and checks the binary actually ran something.
    """
    config_path = Path(temp_dir) / "test-pre-commit-config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    (Path(temp_dir) / "probe.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([require_executable(GIT), "init", "-q", str(temp_dir)], check=True, capture_output=True)
    subprocess.run(
        [require_executable(GIT), "-C", str(temp_dir), "add", "probe.py"], check=True, capture_output=True
    )
    return subprocess.run(
        [require_repo_tool(SERVICE_ROOT, PRE_COMMIT), "run", "--all-files", "--config", str(config_path)],
        cwd=temp_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _assert_no_config_exclude(config: str) -> None:
    """The config root must not declare an `exclude` that bypasses every hook.

    `exclude: .*` in the pre-commit config disables all hooks: every file is
    excluded, every hook reports Skipped, and the binary exits 0. This assertion
    runs the REAL pre-commit binary against a synthetic config that replaces the
    real hooks with `exit 1`, proves the binary exits 0 (hooks skipped), and
    then requires the contract to reject that exclude pattern.
    """
    document = yaml.safe_load(config)
    assert isinstance(document, dict), "the pre-commit config must be a mapping"
    exclude = document.get("exclude")
    if exclude is None:
        return
    assert isinstance(exclude, str), "the pre-commit config exclude must be a string pattern"
    # `.*` matches every file — total disable.
    # `*.py` matches every Python file — disables every Python-scoped hook.
    if exclude in (".*", "*.py", ".*.py$"):
        raise ValueError(
            f"config exclude={exclude!r} disables every hook; "
            "only safe repo dirs (venv, cache, git) may be excluded"
        )


def _field(block: dict, name: str) -> str:
    assert name in block, f"hook is missing {name}"
    return str(block[name])


def _entry(block: dict) -> str:
    """The command pre-commit will execute, with folded scalars already joined.

    The assertion that used to be here (that `entry:` is a `>-` block) was a
    proxy for the thing that matters: pre-commit runs the entry as one command,
    so a literal block would smuggle newlines into it. That is asserted directly.
    """
    entry = block.get("entry")
    assert isinstance(entry, str) and entry.strip(), "hook is missing an entry"
    assert "\n" not in entry, "entry must be a folded scalar, not a literal one"
    return entry


def _require_exactly_one_jscpd_gate(config: str) -> tuple[str, dict]:
    """One duplication gate, by id, whatever the file looks like."""
    hooks = _hook_blocks(config)
    jscpd_hooks = [(hook_id, hook) for hook_id, hook in hooks if hook_id.split("-")[0] == "jscpd"]
    found = [hook_id for hook_id, _ in jscpd_hooks]
    assert len(jscpd_hooks) == 1, f"exactly one jscpd gate is required, found {found}"
    jscpd_id, jscpd = jscpd_hooks[0]
    assert jscpd_id == "jscpd", f"the only jscpd gate must be the whole-service one, found {jscpd_id!r}"
    return jscpd_id, jscpd


def _synthetic_config(exclude: str | None, entry: str = "bash -c 'exit 1'") -> str:
    """A minimal local pre-commit config carrying one always-failing hook."""
    exclude_line = f"exclude: {exclude}\n" if exclude is not None else ""
    return (
        f"{exclude_line}"
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: synthetic\n"
        "        name: synthetic gate\n"
        f"        entry: {entry}\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        files: '\\.py$'\n"
    )


def test_config_exclude_dot_star_makes_hooks_skipped(tmp_path) -> None:
    """REPRO evidence: `exclude: .*` silently disables a failing hook.

    A hook whose entry is `bash -c 'exit 1'` fails if it runs. Under
    `exclude: .*` pre-commit reports `(no files to check)Skipped` and exits 0,
    so the enforcement vanishes with no non-zero code to trip on. This is why a
    root `exclude` is a config-level bypass: it is a shape the contract must
    refuse, because no execution can catch it — the hooks are never run.
    """
    skipped = _run_precommit_on_temp_config(tmp_path, _synthetic_config(".*"))
    assert skipped.returncode == 0, f"skipped hooks exit 0:\n{skipped.stdout}"
    assert "Skipped" in skipped.stdout, f"the hook must be reported Skipped:\n{skipped.stdout}"


def test_config_without_exclude_runs_the_hook_and_fails(tmp_path) -> None:
    """The harness is real pre-commit: without exclude the hook fails."""
    completed = _run_precommit_on_temp_config(tmp_path, _synthetic_config(None))
    assert completed.returncode != 0, (
        f"the synthetic hook must fail when it actually runs:\nstdout:\n{completed.stdout}"
    )
    assert "exit code: 1" in completed.stdout, f"the hook did not run as expected:\n{completed.stdout}"


def test_the_gate_rejects_a_config_exclude_that_disables_every_hook() -> None:
    """Bites: `_assert_no_config_exclude` fires on `exclude: .*`."""
    with pytest.raises(ValueError, match="disables every hook"):
        _assert_no_config_exclude("repos: []\nexclude: .*\n")


def test_the_gate_accepts_a_config_without_a_root_exclude() -> None:
    _assert_no_config_exclude("repos: []\n")


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
    """The aggregate's shell, which the gate depends on and so must be declared.

    The script is a chain of `test` lines. A shell without errexit gives the
    step the exit code of its LAST line, so `test "failure" = "success"` in the
    middle is invisible: the required check reports success over a run that
    failed. GitHub's implicit default on Linux is `bash -e {0}`, so this works
    today by accident, and the accident is exactly what an edit to this step,
    a `defaults.run.shell`, or a move to another runner can take away.
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
    """Reject any `continue-on-error` that GitHub could evaluate to true.

    `continue-on-error` is an expression in GitHub Actions. `yaml.safe_load`
    gives `${{ true }}` back as the STRING "${{ true }}", so an `is not True`
    check is satisfied by an expression that GitHub evaluates to true — the
    job/step is reported successful and `needs.X.result` becomes hollow. The
    only value that is safe is absent, or literally the scalar `false`.
    """
    value = block.get("continue-on-error")
    assert value is False or value is None, (
        f"{label} is continue-on-error={value!r}, so needs.result is hollow; "
        "it must be false or absent (expressions are evaluated by GitHub)"
    )


def _assert_failure_propagation(workflow: str) -> None:
    """A required check that cannot fail is not a gate.

    Two evasions were verified against the previous version of this contract,
    which only counted the enforcement command and checked that `enforcement`
    was in `needs`:

      * `continue-on-error: true` on the enforcement job -- GitHub reports the
        job as successful, so the aggregate asserts success over a run that
        never happened.
      * deleting `test "${{ needs.enforcement.result }}" = "success"` -- the
        aggregate still runs, still needs enforcement, and no longer checks it.

    Both are refused here, and the refusal is checked by executing the
    aggregate's own script rather than by reading it.
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
    # The floor is on the command line rather than inside the gate so the
    # command stays the one place a reviewer looks to see what duplication costs.
    # One floor for every root: the split it replaced existed only while a
    # loose test floor was hiding production duplication, and a second number
    # would be a second thing to ratchet.
    #
    # The numbers are read from the hook rather than spelled here. This assertion
    # used to carry its own copy of the pair, which is a fourth restatement of a
    # number that lives in the entry it is checking: lowering the floor would
    # have left the config correct and this test red, and raising it would have
    # done the reverse. Asserting the shape -- a floor is stated, it is one pair,
    # and it is the enforced one -- is what survives a floor change.
    floor = re.search(r"--min-lines (\d+) --min-tokens (\d+) --threshold (\S+)", entry)
    assert floor is not None, f"the entry states no single floor triple: {entry!r}"
    assert floor.group(1) == str(gate.configured_floor()[0])
    assert floor.group(2) == str(gate.configured_floor()[1])
    assert float(floor.group(3)) == gate.configured_floor()[2]
    assert "--app-min-lines" not in entry, (
        "the per-tree floor split is gone; one number must serve both trees"
    )
    assert _field(jscpd, "files") == ENFORCEMENT_TRIGGER_PATTERN

    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    assert all("profiles" not in pair for pair in expectations["pairs"])

    assert workflow.count(ENFORCEMENT_COMMAND) == 1
    assert "scripts/jscpd_gate.py" not in workflow
    required = _required_needs(workflow)
    assert required == REPORTING_JOBS
    assert "pre-commit==4.6.2" in (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    _assert_failure_propagation(workflow)


def test_no_ci_job_reruns_a_precommit_hook_command() -> None:
    """CI must not execute a gate twice inside one run.

    `lint` (ruff) and `typecheck` (mypy + pyright) used to be separate jobs that
    re-ran exactly what the pre-commit hooks inside `enforcement` already
    execute -- three gates ran twice per CI run and nothing compared the copies.
    This is the chokepoint that would have caught that duplication on day one:
    any `run:` command in a workflow job must not contain a command a pre-commit
    hook already runs. The hook entries strip the shared `bash -c 'cd...' gate`
    wrapper; the tool invocation after it is what CI must not repeat.
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
    """No CI job re-runs a command the pre-commit enforcement job already runs.

    The duplicate and the gate are two copies of the same check, and only the
    copy that is edited drifts. The error names the job so the offending step is
    findable from the failure alone.
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


# How a gate tool may be spelled in a Makefile recipe, as a pattern rather than
# a literal string. The interpreter prefix is deliberately not part of the match:
# `$(PYTHON) -m ruff` and `.venv/bin/python -m ruff` are the same gate, and
# pinning either spelling would let the other through -- which is the exact shape
# of the drift this detects, since the two copies differed in their prefix and in
# their arguments at once.
#
# `pytest` is absent on purpose. The hooks run pytest SUBSETS (the pyright budget
# test, the safety invariants), while `make test` runs the whole suite, which is
# CI's separate test surface rather than a duplicate of a hook. Requiring that to
# delegate by id would be wrong, so the rule is scoped to the tools a hook owns
# outright.
GATE_TOOL_PATTERNS = (
    r"-m\s+ruff\b",
    r"-m\s+mypy\b",
    r"scripts/jscpd_gate\b",
    r"test_pyright_budget\.py\b",
    r"tests/invariants/\b",
)


# How a Makefile recipe names a hook: either the resolved binary (`pre-commit`)
# or the Makefile's own indirection to it. The variable is written `PRE_COMMIT`
# and closed with `)`, so a pattern for the bare word would never match it and
# every delegation would read as no delegation at all.
MAKEFILE_HOOK_ID = re.compile(r"(?:\$\(PRE_COMMIT\)|\bpre-commit)\s+run\s+(\S+)")


def _makefile_recipe_lines(makefile: str) -> list[tuple[str, str]]:
    """`(target, command)` for every non-comment recipe line in a Makefile.

    Comments are dropped because they are prose about a command rather than the
    command: this file's own comment records the ruff invocation it no longer
    runs, and a detector that read that would be reporting the explanation of the
    fix as the defect. A `Makefile` is tab-indented, and a line starting with a
    non-tab is a target or a variable.
    """
    lines: list[tuple[str, str]] = []
    target = ""
    for raw in makefile.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw[0].isspace():
            target = stripped.rstrip(":").strip()
            continue
        lines.append((target, stripped))
    return lines


def _assert_no_gate_restated(makefile: str) -> None:
    """No Makefile recipe runs a gate tool directly instead of by hook id.

    The Makefile used to spell out `ruff check app tests scripts` and `mypy`,
    which made it a second declaration of every gate -- and the two copies had
    already drifted apart in both scope and interpreter before this was checked.
    Delegating with `pre-commit run <id>` deletes the second copy rather than
    documenting it, and the commands then live only in the config CI also reads.
    """
    for target, command in _makefile_recipe_lines(makefile):
        for pattern in GATE_TOOL_PATTERNS:
            assert not re.search(pattern, command), (
                f"make target {target!r} runs a gate tool directly ({command!r}): "
                "run it as `pre-commit run <hook-id>` so the command stays in "
                ".pre-commit-config.yaml, which is the only declaration of it"
            )


def test_makefile_runs_gates_through_hook_ids_rather_than_restating_them() -> None:
    _assert_no_gate_restated(MAKEFILE_PATH.read_text(encoding="utf-8"))


def test_a_makefile_target_that_restates_a_hook_command_is_caught() -> None:
    """Bites: putting a gate command back in a recipe fails the check."""
    makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
    broken = makefile.replace(
        'cd "$(SUITE_DIR)" && $(PRE_COMMIT) run ruff --all-files --config "$(HOOK_CONFIG)"',
        'cd "$(SUITE_DIR)" && $(PYTHON) -m ruff check app tests scripts',
    )
    assert broken != makefile, "the delegation line this test rewrites is gone"
    with pytest.raises(AssertionError):
        _assert_no_gate_restated(broken)


def test_every_hook_id_the_makefile_names_exists() -> None:
    """A delegation is only a single source of truth if the id resolves.

    Delegating by id replaces one drift with a quieter one: a renamed hook would
    leave `make lint` reporting "hook not found" as a non-zero exit that reads
    like a passing gate that did nothing.
    """
    config = CONFIG_PATH.read_text(encoding="utf-8")
    known = {hook_id for hook_id, _ in _hook_blocks(config)}
    referenced = {
        hook_id
        for _target, command in _makefile_recipe_lines(MAKEFILE_PATH.read_text(encoding="utf-8"))
        for hook_id in MAKEFILE_HOOK_ID.findall(command)
    }
    assert referenced, "the Makefile is expected to delegate at least one gate by hook id"
    assert referenced <= known, (
        f"the Makefile names hook ids the config does not declare: "
        f"{sorted(referenced - known)}; declared ids are {sorted(known)}"
    )


def test_a_makefile_target_naming_an_unknown_hook_id_is_caught() -> None:
    """Bites: a stale hook id fails the check rather than exiting quietly."""
    makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
    broken = makefile.replace("$(PRE_COMMIT) run mypy", "$(PRE_COMMIT) run mypy-renamed")
    assert broken != makefile, "the delegation line this test rewrites is gone"
    known = {hook_id for hook_id, _ in _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))}
    referenced = {
        hook_id
        for _target, command in _makefile_recipe_lines(broken)
        for hook_id in MAKEFILE_HOOK_ID.findall(command)
    }
    assert referenced - known == {"mypy-renamed"}


def _evasive_workflow(evasion: str) -> str:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    if evasion == "continue-on-error":
        mutated = workflow.replace(
            "  enforcement:\n    name: Pre-commit enforcement\n",
            "  enforcement:\n    name: Pre-commit enforcement\n    continue-on-error: true\n",
            1,
        )
    elif evasion == "step-continue-on-error":
        mutated = workflow.replace(
            "      - name: Run all configured hooks\n",
            "      - name: Run all configured hooks\n        continue-on-error: true\n",
            1,
        )
    elif evasion == "expression-continue-on-error":
        # GitHub evaluates `continue-on-error` as an expression. `yaml.safe_load`
        # turns `${{ true }}` into the STRING "${{ true }}", which `is not True`,
        # so a `is not True` check lets it through — then GitHub runs the job as
        # successful and `needs.X.result` is hollow.
        mutated = workflow.replace(
            "      - name: Run all configured hooks\n",
            "      - name: Run all configured hooks\n        continue-on-error: ${{ true }}\n",
            1,
        )
    elif evasion == "dropped-assertion":
        mutated = workflow.replace("          " + _result_assertion("enforcement") + "\n", "", 1)
    elif evasion == "dropped-aggregate-need":
        mutated = workflow.replace(
            "needs: [lint, typecheck, enforcement, test-fast]", "needs: [lint, typecheck, test-fast]", 1
        )
    elif evasion == "dropped-reporting-job":
        # Drop a REPORTING job (not the aggregate) from `needs:` and its
        # result assertion. This is the narrower-shape lie: the required
        # aggregate still depends on enforcement, and every remaining need
        # is asserted, but one reporting job is now invisible to it.
        mutated = workflow.replace(
            "needs: [lint, typecheck, enforcement, test-fast]", "needs: [lint, enforcement, test-fast]", 1
        )
        mutated = mutated.replace("          " + _result_assertion("typecheck") + "\n", "", 1)
    elif evasion == "no-always":
        mutated = workflow.replace("    if: ${{ always() }}\n", "", 1)
    elif evasion == "no-errexit":
        mutated = workflow.replace("        shell: " + ERREXIT_SHELL + "\n", "", 1)
    elif evasion == "sh-shell":
        mutated = workflow.replace("        shell: " + ERREXIT_SHELL + "\n", "        shell: sh\n", 1)
    else:
        raise AssertionError(f"unknown evasion: {evasion}")
    assert mutated != workflow, f"the {evasion} mutation did not apply, so this test would prove nothing"
    return mutated


@pytest.mark.parametrize(
    "evasion",
    [
        "continue-on-error",
        "step-continue-on-error",
        "expression-continue-on-error",
        "dropped-assertion",
        "dropped-aggregate-need",
        "no-always",
        "no-errexit",
        "dropped-reporting-job",
        "sh-shell",
    ],
)
def test_required_gate_evasions_are_rejected(evasion) -> None:
    with pytest.raises(AssertionError):
        _assert_failure_propagation(_evasive_workflow(evasion))


def test_the_aggregate_script_is_what_fails_not_just_what_it_says() -> None:
    """The enforcement assertion is the load-bearing line; prove it is executed.

    Before this, the contract only read the workflow. Reading it is what let
    both evasions through, so the propagation claim is settled by running the
    aggregate's own script with a failed need.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    needs = _required_needs(workflow)
    script = _required_gate_script(workflow)
    assert _required_gate_shell(workflow) == ERREXIT_SHELL
    assert _run_script(_render_results(script, {job: "success" for job in needs})).returncode == 0
    failed = _run_script(
        _render_results(script, dict.fromkeys(needs, "success") | {"enforcement": "failure"})
    )
    assert failed.returncode != 0, "a failed enforcement job must fail the required aggregate"
    # The same script under a shell without errexit reports success, which is why
    # the declared shell above is a gate and not a style preference.
    without_errexit = subprocess.run(
        [
            require_executable(BASH),
            "-c",
            _render_results(script, dict.fromkeys(needs, "success") | {"enforcement": "failure"}),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert without_errexit.returncode == 0, (
        "this test is only meaningful if the chain is errexit-sensitive; it no longer is"
    )


def test_quoted_hook_id_is_visible_to_this_contract() -> None:
    """A hook id is a YAML value, not a line of text.

    The parser used to be `^      - id: ([A-Za-z0-9_-]+)\\s*$`, so a hook
    declared `- id: "jscpd-legacy"` did not exist as far as this contract was
    concerned and a second, laxer duplication gate could be added undetected.
    """
    hooks = _hook_blocks(QUOTED_SECOND_JSCPD_HOOK)
    assert [hook_id for hook_id, _ in hooks] == ["jscpd", "jscpd-legacy"]


def test_a_second_quoted_jscpd_hook_is_rejected() -> None:
    with pytest.raises(AssertionError):
        _require_exactly_one_jscpd_gate(QUOTED_SECOND_JSCPD_HOOK)


def test_all_hooks_share_one_trigger() -> None:
    """The five `files:` values are one YAML anchor, so they cannot drift apart.

    The trigger was copied five times previously, and the copies were never
    compared: it is what let the `.gitignore` edit slip through with every hook
    Skipped. They are now `&service_trigger` / `*service_trigger`, so a divergent
    value on any one hook is caught here.
    """
    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    triggers = {_field(block, "files") for _, block in hooks}
    assert triggers == {ENFORCEMENT_TRIGGER_PATTERN}, (
        f"hooks must all share the one trigger {ENFORCEMENT_TRIGGER_PATTERN!r}; got {sorted(triggers)}"
    )


def test_a_hook_with_a_divergent_trigger_is_caught() -> None:
    """Bites: a hook carrying a different `files:` value fails the all-share-one check.

    The anchor collapses the copies, but the chokepoint is the assertion here:
    without it, adding a sixth hook (or reverting one copy to a per-file pattern)
    would silently reintroduce the drift this file exists to forbid.
    """
    config = CONFIG_PATH.read_text(encoding="utf-8")
    reverted = config.replace("*service_trigger", r"^(docker/browser-crawler/)?.*\.py$")
    reverted_hooks = _hook_blocks(reverted)
    assert _field(reverted_hooks[0][1], "files") == ENFORCEMENT_TRIGGER_PATTERN
    assert _field(reverted_hooks[-1][1], "files") == r"^(docker/browser-crawler/)?.*\.py$"


def test_every_gate_fires_on_every_enforcement_input() -> None:
    """No gate's own configuration can be edited without that gate running.

    The `files:` pattern is a pre-commit trigger, so a config that no pattern
    matches is a config nothing checks. `.jscpd.json` was the live instance: every
    hook matched only `.*\\.py`, so the commit that added an ignore glob to hide a
    clone ran no hook at all.
    """
    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    assert hooks
    for hook_id, block in hooks:
        pattern = _field(block, "files")
        for name in ENFORCEMENT_INPUTS:
            # Both layouts: nested under the parent repo, and at the repository
            # root once the service is extracted.
            for candidate in (f"docker/browser-crawler/{name}", name):
                assert re.search(pattern, candidate), f"hook {hook_id} does not fire on {candidate}"


def test_the_workflow_is_written_for_the_repository_root_it_will_run_from() -> None:
    """GitHub only loads workflows from a repository root.

    This file is nested inside the service today, so it has never run and is
    currently documentation. It is written for the layout where the service is
    the repository, which is where it is going: the extraction is a file copy,
    so every path in it has to already be true at a root. A `docker/browser-crawler/`
    prefix is the one thing that cannot survive that copy, because it names a
    directory that will not exist there.
    """
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
    """Every job needs `fetch-depth: 0` for the git-history budget ratchet.

    tests/test_metrics_budget.py reads this service's commit history to prove
    the complexity budgets only fall. GitHub's default checkout is a shallow
    clone of depth 1, under which the ratchet test SKIPS -- a gate that is
    silently inert in CI. Each job's checkout must fetch the full history.
    """
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

    The dual-layout resolution used to be five VERBATIM copies of a two-line
    preamble inlined in each entry, checked by substring-matching that preamble.
    It now lives in one committed helper, ``scripts/gate``, and the entries keep
    only the irreducible part: a file that is at ``docker/browser-crawler/
    scripts/gate`` in one layout and at ``scripts/gate`` in the other cannot be
    named by a single relative path from the repository root, which is where
    pre-commit runs every hook.

    So the property is asserted where it now lives, and by EXECUTION rather than
    by reading the config. The contract builds a synthetic git repository in each
    layout, installs the REAL helper and the REAL entry in it, runs them, and
    requires the gate to end up in that layout's service root. Losing the
    fallback makes the root layout exit non-zero, which is the failure the old
    substring assertion was only a proxy for.
    """
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

    The helper moved the semantic content out of the config, but the one-line
    lookup that remains is still copied five times -- YAML has no string
    interpolation, so this cannot be an anchor the way ``*service_trigger`` is.
    What it CAN be is checked: every entry must open with the same bytes, so
    editing one preamble and not the others fails here instead of quietly
    working in one layout and not the other.
    """
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

    pre-commit shlex-splits the entry into ``bash -c SCRIPT gate ARGS...``, so
    ``SCRIPT`` is the whole dual-layout prologue and ``ARGS`` is the gate command
    the helper hands to the pinned interpreter. The split point is the closing
    quote of that script followed by the gate's own ``$0`` name, which appears
    exactly once per entry.
    """
    prologue, separator, command = entry.partition(GATE_ENTRY_END)
    assert separator, f"gate entry has no {GATE_ENTRY_END!r} terminator: {entry!r}"
    return prologue + "'", command.strip()


def _run_gate_entry_in_layout(repo: Path, layout: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Run a real hook entry against a synthetic repo laid out the named way.

    ``nested`` builds ``<repo>/docker/browser-crawler`` (the service vendored
    into a parent monorepo). ``root`` builds the service at the repository root,
    which is the layout this repository is in. The helper and the entry are the
    real files rather than re-typed stand-ins, so a helper that stops doing its
    job cannot pass this by agreeing with a copy of itself.
    """
    # `browser-crawler` here is the pre-extraction layout the hook's prologue
    # still probes for, not this project's name; the nested branch exists to
    # prove the helper resolves there too.
    service_root = repo / "docker" / "browser-crawler" if layout == "nested" else repo
    subprocess.run([require_executable(GIT), "init", "-q", str(repo)], check=True, capture_output=True)
    (service_root / "scripts").mkdir(parents=True)
    (service_root / "scripts" / "gate").write_text(GATE_HELPER.read_text(encoding="utf-8"), encoding="utf-8")
    (service_root / "scripts" / "gate").chmod(0o755)
    # The helper execs the command it is handed; the synthetic service has no
    # venv of its own, so the command names the stub interpreter directly.
    (service_root / ".venv" / "bin").mkdir(parents=True)
    interpreter = service_root / ".venv" / "bin" / "python"
    interpreter.write_text('#!/bin/sh\nexec python3 "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    subprocess.run([require_executable(GIT), "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    # pre-commit runs an entry as `shlex.split(entry)` with the repository root
    # as the working directory, so the first four tokens -- program, -c, the
    # dual-layout script, and the gate's own $0 name -- are exactly what has to
    # work. Only the command AFTER them is swapped for one this synthetic
    # service can satisfy (it prints its cwd, which is the whole question).
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
