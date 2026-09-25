"""The pre-commit config is the single CI enforcement surface."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
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
ENFORCEMENT_COMMAND = (
    f"pre-commit run --all-files --config {WORKFLOW_ROOT_CONFIG}"
)
REPORTING_JOBS = {"lint", "typecheck", "test-fast"}
# GitHub's template form for an errexit shell. The required aggregate is a chain
# of `test` lines, so its exit code is its last line's without errexit.
ERREXIT_SHELL = "bash -e {0}"
# Every file the gates read to decide what to enforce. A commit that edits one of
# them must run the enforcement: the config that decides what is checked cannot
# itself be the one change that skips the check.
ENFORCEMENT_INPUTS = (
    "app/settings.py",
    "tests/test_jscpd_gate.py",
    "scripts/module_graph.py",
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
# The single trigger pattern every hook carries. Optional leading segment so the
# same pattern matches nested under the parent repo and at the repository root
# once the service is extracted.
ENFORCEMENT_TRIGGER_PATTERN = (
    r"^(docker/browser-crawler/)?(.*\.py|\.pre-commit-config\.yaml|\.jscpd\.json"
    r"|jscpd-expectations\.json|pyrightconfig\.json|mypy\.ini|pytest\.ini"
    r"|pyproject\.toml|requirements-dev\.txt|requirements\.txt|\.github/workflows/.*\.ya?ml)$"
)
# A syntactically valid config carrying a second, quoted jscpd hook that would
# pass a lax threshold. Nothing distinguishes it from the real config to a
# reader of the raw text except the quotes.
QUOTED_SECOND_JSCPD_HOOK = """\
repos:
  - repo: local
    hooks:
      - id: jscpd
        name: jscpd
        entry: .venv/bin/python tests/_jscpd_gate.py all
        language: system
        files: ^(docker/browser-crawler/)?.*\\.py$
        pass_filenames: false
        stages: [pre-commit]
      - id: "jscpd-legacy"
        name: jscpd legacy
        entry: jscpd --threshold 40 app tests || true
        language: system
        files: ^(docker/browser-crawler/)?.*\\.py$
        pass_filenames: false
        stages: [pre-commit]
"""


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
    return subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, check=False)


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
    assert required.get("continue-on-error") is not True, "the required aggregate cannot be continue-on-error"
    assert required.get("if") == "${{ always() }}", "the required aggregate must run even when a need fails"
    for name, job in jobs.items():
        assert job.get("continue-on-error") is not True, f"job {name} is continue-on-error, so needs.{name}.result is hollow"
        for step in job.get("steps") or []:
            assert step.get("continue-on-error") is not True, f"a step in {name} is continue-on-error"

    needs = _required_needs(workflow)
    assert "enforcement" in needs, "the required aggregate must depend on enforcement"
    assert REPORTING_JOBS <= needs, (
        f"the required aggregate must depend on every reporting job, "
        f"missing {REPORTING_JOBS - needs}"
    )
    _required_gate_shell(workflow)
    script = _required_gate_script(workflow)
    for job in sorted(needs):
        assert _result_assertion(job) in script, f"the required aggregate never asserts needs.{job}.result succeeded"

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

    jscpd_id, jscpd = _require_exactly_one_jscpd_gate(config)
    assert jscpd_id == "jscpd"
    assert "jscpd-app" not in config
    assert "jscpd-tests" not in config
    assert not [hook_id for hook_id, _ in hooks if hook_id in {"jscpd-app", "jscpd-tests"}]

    assert all("pre-push" not in (block.get("stages") or []) for _, block in hooks)
    assert all(block.get("stages") == ["pre-commit"] for _, block in hooks)

    entry = _entry(jscpd)
    assert ".venv/bin/python tests/_jscpd_gate.py all" in entry
    assert "--min-lines 8" in entry
    assert "--min-tokens 40" in entry
    # The production tree carries its own, tighter floor, and it is on the
    # command line rather than in the gate so the command stays the one place a
    # reviewer looks to see what duplication costs.
    assert "--app-min-lines 7" in entry
    assert "--app-min-tokens 35" in entry
    assert "--threshold 0" in entry
    production = re.search(r"--app-min-lines (\d+) --app-min-tokens (\d+)", entry)
    assert production is not None
    support = re.search(r"--min-lines (\d+) --min-tokens (\d+)", entry)
    assert support is not None
    assert int(production.group(1)) < int(support.group(1)), "the production floor must be the tighter one"
    assert _field(jscpd, "files") == ENFORCEMENT_TRIGGER_PATTERN

    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    assert all("profiles" not in pair for pair in expectations["pairs"])

    assert workflow.count(ENFORCEMENT_COMMAND) == 1
    assert "tests/_jscpd_gate.py" not in workflow
    required = _required_needs(workflow)
    assert "enforcement" in required
    assert required - {"enforcement"} == REPORTING_JOBS
    assert "pre-commit==4.6.2" in (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    _assert_failure_propagation(workflow)


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
    elif evasion == "dropped-assertion":
        mutated = workflow.replace('          ' + _result_assertion("enforcement") + "\n", "", 1)
    elif evasion == "dropped-aggregate-need":
        mutated = workflow.replace("needs: [lint, typecheck, enforcement, test-fast]", "needs: [lint, typecheck, test-fast]", 1)
    elif evasion == "dropped-reporting-job":
        # Drop a REPORTING job (not the aggregate) from `needs:` and its
        # result assertion. This is the narrower-shape lie: the required
        # aggregate still depends on enforcement, and every remaining need
        # is asserted, but one reporting job is now invisible to it.
        mutated = workflow.replace("needs: [lint, typecheck, enforcement, test-fast]", "needs: [lint, enforcement, test-fast]", 1)
        mutated = mutated.replace('          ' + _result_assertion("typecheck") + "\n", "", 1)
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
    failed = _run_script(_render_results(script, dict.fromkeys(needs, "success") | {"enforcement": "failure"}))
    assert failed.returncode != 0, "a failed enforcement job must fail the required aggregate"
    # The same script under a shell without errexit reports success, which is why
    # the declared shell above is a gate and not a style preference.
    without_errexit = subprocess.run(
        ["bash", "-c", _render_results(script, dict.fromkeys(needs, "success") | {"enforcement": "failure"})],
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


def test_every_hook_resolves_the_service_root_in_both_layouts() -> None:
    """The config travels, so its commands must work at either root.

    Each entry cds to `<repo root>/docker/browser-crawler` and falls back to the
    repository root when that directory is not there. That fallback is the only
    reason one config file governs the service nested in dev-stack today and at
    the root of the extracted repository, so it is asserted rather than assumed:
    losing it makes every hook fail on its first cd in one of the two layouts.
    """
    for hook_id, block in _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8")):
        entry = _entry(block)
        assert 'git rev-parse --show-toplevel)/docker/browser-crawler' in entry, (
            f"hook {hook_id} does not try the nested service path"
        )
        assert "||" in entry and 'cd "$(git rev-parse --show-toplevel)"' in entry, (
            f"hook {hook_id} has no fallback to the repository root, so it cannot run "
            "at the root of the extracted repository"
        )
