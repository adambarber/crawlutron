"""The Makefile invokes gates by hook id, never by restated command."""

from __future__ import annotations

import re
import subprocess

import pytest

from scripts.executables import BASH, require_executable
from tests._ci_contract import (
    CONFIG_PATH,
    ERREXIT_SHELL,
    MAKEFILE_PATH,
    QUOTED_SECOND_JSCPD_HOOK,
    WORKFLOW_PATH,
    _assert_failure_propagation,
    _hook_blocks,
    _render_results,
    _require_exactly_one_jscpd_gate,
    _required_gate_script,
    _required_gate_shell,
    _required_job,
    _required_needs,
    _run_script,
)

# How a gate tool may be spelled in a Makefile recipe, as a pattern rather than a literal string. `pytest` is
# absent on purpose.
GATE_TOOL_PATTERNS = (
    r"-m\s+ruff\b",
    r"-m\s+mypy\b",
    r"scripts/jscpd_gate\b",
    r"basedpyright\b",
    r"tests/invariants/\b",
)


# How a Makefile recipe names a hook: either the resolved binary (`pre-commit`) or the Makefile's own
# indirection to it.
MAKEFILE_HOOK_ID = re.compile(r"(?:\$\(PRE_COMMIT\)|\bpre-commit)\s+run\s+(\S+)")


def _makefile_recipe_lines(makefile: str) -> list[tuple[str, str]]:
    """`(target, command)` for every non-comment recipe line. Comments are prose
    about a command, not the command -- this file's own comment records the ruff
    invocation it no longer runs, so reading it would report the fix as the defect.
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
    """No recipe runs a gate tool directly instead of by hook id: the Makefile once
    spelled out `ruff check` and `mypy`, a second declaration of every gate that had
    already drifted apart -- so delegation deletes the copy, not documents it.
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
    """A delegation is single-sourced only if the id resolves: a renamed hook would
    leave `make lint` exiting non-zero with "hook not found", which reads like a
    passing gate that did nothing.
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
    """Return ci.yml with one required-gate evasion applied; each needle is
    confirmed present in the live workflow before it is mutated."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    required = _required_job(workflow)
    needs_raw = required.get("needs")
    assert isinstance(needs_raw, list) and needs_raw, "the required aggregate must declare needs"
    needs = [str(job) for job in needs_raw]
    first_need = needs[0]
    mutations = {
        "continue-on-error": (
            "  required:\n    name: Required quality gate\n",
            "  required:\n    name: Required quality gate\n    continue-on-error: true\n",
        ),
        "step-continue-on-error": (
            "      - name: Require every gate to have succeeded\n",
            "      - name: Require every gate to have succeeded\n        continue-on-error: true\n",
        ),
        "expression-continue-on-error": (
            "      - name: Require every gate to have succeeded\n",
            "      - name: Require every gate to have succeeded\n        continue-on-error: ${{ true }}\n",
        ),
        "dropped-assertion": (
            '          test "${{ needs.actionlint.result }}" = "success"\n',
            "",
        ),
        "dropped-aggregate-need": (f"      - {first_need}\n", ""),
        "dropped-reporting-job": (f"      - {first_need}\n", ""),
        "no-always": (
            "  required:\n    name: Required quality gate\n    if: ${{ always() }}\n",
            "  required:\n    name: Required quality gate\n",
        ),
        "no-errexit": ("        shell: " + ERREXIT_SHELL + "\n", ""),
        "sh-shell": ("        shell: " + ERREXIT_SHELL + "\n", "        shell: sh\n"),
    }
    if evasion not in mutations:
        raise AssertionError(f"unknown evasion: {evasion}")
    needle, replacement = mutations[evasion]
    assert needle in workflow, f"{evasion}: needle absent from live ci.yml"
    mutated = workflow.replace(needle, replacement, 1)
    if evasion == "dropped-reporting-job":
        # Drop one real reporting job from needs and its result assertion.
        assertion = '          test "${{ needs.' + first_need + '.result }}" = "success"\n'
        assert assertion in workflow, f"{evasion}: assertion needle absent from live ci.yml"
        mutated = mutated.replace(assertion, "", 1)
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
    mutated = _evasive_workflow(evasion)  # bite applied for real: guard outside raises
    with pytest.raises(AssertionError):
        _assert_failure_propagation(mutated)  # failure propagation asserted separately


def test_stale_needle_cannot_slip_the_bite() -> None:
    """A needle absent from live ci.yml fails the bite instead of proving
    nothing: the non-vacuity guard reds when the mutation never applies."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    stale = "needs: [lint, typecheck, enforcement, test-fast]"
    assert stale not in workflow
    mutated = workflow.replace(stale, "needs: [lint, typecheck, test-fast]", 1)
    assert mutated == workflow
    with pytest.raises(AssertionError, match="did not apply"):
        assert mutated != workflow, "the stale-needle mutation did not apply"


def test_the_aggregate_script_is_what_fails_not_just_what_it_says() -> None:
    """The enforcement assertion is the load-bearing line and must execute: the
    contract once only READ the workflow, which is what let both evasions through,
    so the propagation claim is settled by running the aggregate script.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    needs = _required_needs(workflow)
    script = _required_gate_script(workflow)
    assert _required_gate_shell(workflow) == ERREXIT_SHELL
    assert _run_script(_render_results(script, {job: "success" for job in needs})).returncode == 0
    failed = _run_script(_render_results(script, dict.fromkeys(needs, "success") | {"battery": "failure"}))
    assert failed.returncode != 0, "a failed battery job must fail the required aggregate"
    # The same script under a shell without errexit reports success, which is why the declared shell above is
    # a gate and not a style preference.
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
    """A hook id is a YAML value, not a line: the old anchored regex saw `- id:
    "jscpd-legacy"` as nonexistent, so a second, laxer duplication gate could be
    added undetected.
    """
    hooks = _hook_blocks(QUOTED_SECOND_JSCPD_HOOK)
    assert [hook_id for hook_id, _ in hooks] == ["jscpd", "jscpd-legacy"]


def test_a_second_quoted_jscpd_hook_is_rejected() -> None:
    with pytest.raises(AssertionError):
        _require_exactly_one_jscpd_gate(QUOTED_SECOND_JSCPD_HOOK)
