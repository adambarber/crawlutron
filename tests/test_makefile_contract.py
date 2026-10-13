"""The Makefile invokes gates by hook id, never by restated command."""

import re
import subprocess

import pytest

from scripts.executables import BASH, require_executable
from tests.test_ci_enforcement import (
    _assert_failure_propagation,
    _render_results,
    _required_gate_script,
    _required_gate_shell,
    _required_needs,
    _result_assertion,
    _run_script,
)
from tests.test_precommit_ci_contract import (
    CONFIG_PATH,
    ERREXIT_SHELL,
    MAKEFILE_PATH,
    QUOTED_SECOND_JSCPD_HOOK,
    WORKFLOW_PATH,
    _hook_blocks,
    _require_exactly_one_jscpd_gate,
)

# How a gate tool may be spelled in a Makefile recipe, as a pattern rather than a literal string. `pytest` is
# absent on purpose.
GATE_TOOL_PATTERNS = (
    r"-m\s+ruff\b",
    r"-m\s+mypy\b",
    r"scripts/jscpd_gate\b",
    r"test_pyright_budget\.py\b",
    r"tests/invariants/\b",
)


# How a Makefile recipe names a hook: either the resolved binary (`pre-commit`) or the Makefile's own
# indirection to it.
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
        # GitHub evaluates `continue-on-error` as an expression.
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
        # Drop a REPORTING job (not the aggregate) from `needs:` and its result assertion.
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
