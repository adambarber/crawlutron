"""Tests run in a random order, always, and the enforcement is mechanical: the suite
passed in its written order until asked for another, so randomising is enforced from
both sides -- the live run and the gate configs -- because an ad-hoc opt-out is invisible to either alone.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

# The root both path derivations start from: this file is `tests/<name>.py`, so the service root is two
# parents up.
SERVICE_ROOT = Path(__file__).resolve().parent.parent

#: Configs that decide what the gates run, and so can decide that a run does not vary.
ORDER_PINNING_CONFIGS = (
    "pytest.ini",
    ".pre-commit-config.yaml",
    "Makefile",
    "scripts/gate",
    ".github/workflows/ci.yml",
)

#: Ways a run can be pinned: any explicit --randomly-seed value (the plugin's
#: unset default "default" is a fresh random draw), plus the order flags.
_PINNING_SEED_DEFAULT = "default"
_ORDER_PINNING_FLAGS = ("--randomly-dont-reorganize", "--randomly-dont-reset-seed")


def _seed_pinned_by_argv(argv) -> str | None:
    """Return the pinning token if argv pins the order via an explicit seed or flag."""
    for index, token in enumerate(argv):
        if token.startswith("--randomly-seed="):
            value = token.split("=", 1)[1]
            if value != _PINNING_SEED_DEFAULT:
                return token
        elif token == "--randomly-seed":
            if index + 1 < len(argv) and argv[index + 1] != _PINNING_SEED_DEFAULT:
                return f"--randomly-seed {argv[index + 1]}"
        elif token in _ORDER_PINNING_FLAGS:
            return token
    return None


def _effective_argv(pytestconfig: pytest.Config) -> list[str]:
    """Every way pytest assembles its command line: argv, PYTEST_ADDOPTS, ini addopts.
    A pin delivered anywhere but argv is invisible to invocation_params.args.
    """
    sources: list[str] = list(pytestconfig.invocation_params.args)
    sources.extend(shlex.split(os.environ.get("PYTEST_ADDOPTS", "")))
    try:
        addopts = pytestconfig.getini("addopts")
    except (KeyError, ValueError):
        addopts = ""
    sources.extend(addopts if isinstance(addopts, list) else shlex.split(str(addopts)))
    return sources


def test_this_run_was_randomized(pytestconfig: pytest.Config) -> None:
    """Fail unless the running process is actually varying test order: ``randomly_seed``
    defaults to ``"default"`` when the plugin loads and is absent under `-p no:randomly`,
    and ``randomly_reorganize`` catches `--randomly-dont-reorganize`, which leaves it present.
    """
    seed = pytestconfig.getoption("randomly_seed", None)
    if seed is None:
        pytest.fail(
            "this run loaded no random-order plugin, so it proved nothing about "
            "order independence. pytest-randomly is a dev dependency and is on "
            "by default; something passed `-p no:randomly`."
        )
    reorganize = pytestconfig.getoption("randomly_reorganize", True)
    if not reorganize:
        pytest.fail(
            "this run was seeded but its order was left as declared, so the seed "
            "varied and the ordering did not. That is the flag that looks like it "
            "is enabling randomization."
        )
    pinned = _seed_pinned_by_argv(_effective_argv(pytestconfig))
    if pinned is not None:
        pytest.fail(
            f"this run pinned its order via {pinned!r}: the seed is fixed, not "
            "fresh, so the order did not vary. Repro runs deselect this test by "
            "NAME, never with a seed or order flag."
        )


def _subprocess_gate_run(tmp_path, pin) -> str:
    """Run the gate test in a subprocess, delivering the pin via env or ini addopts."""
    gate = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
        "from tests.test_run_ordering import test_this_run_was_randomized\n"
    )
    probe = tmp_path / "probe_test.py"
    probe.write_text(gate)
    environ = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    if isinstance(pin, tuple) and pin[0] == "env":
        environ["PYTEST_ADDOPTS"] = pin[1]
        pin = None
    if isinstance(pin, str):
        ini = "[pytest]\naddopts = " + pin + "\n"
        (tmp_path / "pytest.ini").write_text(ini)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=100,
        cwd=tmp_path,
    )
    return result.stdout + result.stderr


def _gate_refused(output: str) -> bool:
    """Any of the gate's refusal doors: explicit pin, no-reorganize, no-plugin."""
    return (
        "pinned its order via" in output
        or "order was left as declared" in output
        or "loaded no random-order plugin" in output
    )


def test_numeric_seed_pins_are_seen(tmp_path) -> None:
    """A fixed numeric seed replays one interleaving as surely as `last` does: the
    plugin's unset default is a fresh draw, so ANY explicit value pins the order."""
    for pin in (
        ("env", "--randomly-seed=12345"),
        ("env", "--randomly-seed 12345"),
        "--randomly-seed=12345",
    ):
        output = _subprocess_gate_run(tmp_path, pin)
        assert "pinned its order via" in output, f"{pin!r} numeric seed escaped the gate"


def test_space_form_seed_in_env_is_tokenized_like_pytest_does(tmp_path) -> None:
    """PYTEST_ADDOPTS is a raw shell-ish string: pytest shlex-splits it before
    use, so the gate must too — `--randomly-seed 12345` unsplit is one token no
    matcher branch can see."""
    output = _subprocess_gate_run(tmp_path, ("env", "--randomly-seed 12345"))
    assert "pinned its order via" in output, "space-form seed in PYTEST_ADDOPTS escaped"


def test_order_pinning_flags_are_seen(tmp_path) -> None:
    """--randomly-dont-reorganize and --randomly-dont-reset-seed are named bans in
    AGENTS.md; a smuggled copy must red the gate through any channel."""
    for flag in ("--randomly-dont-reorganize", "--randomly-dont-reset-seed"):
        for pin in (("env", flag), flag):
            output = _subprocess_gate_run(tmp_path, pin)
            assert _gate_refused(output), f"{flag} via {pin!r} escaped the gate"


def test_list_valued_ini_addopts_pin_is_seen(tmp_path) -> None:
    """getini returns addopts as a list; a list-typed pin channel must be swept
    element-wise, not stringified (str(list) breaks the token match)."""
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --randomly-seed=last -x\n")
    output = _subprocess_gate_run(tmp_path, None)
    assert "pinned its order via" in output, "multi-token ini addopts pin escaped the gate"


def test_default_seed_word_stays_legal(tmp_path) -> None:
    """--randomly-seed=default is the plugin's own unset form: it must NOT red."""
    output = _subprocess_gate_run(tmp_path, ("env", "--randomly-seed=default"))
    assert "pinned its order via" not in output, "--randomly-seed=default wrongly banned"


def test_no_enforcement_config_pins_the_order() -> None:
    """Fail if any config that invokes the suite can pin its order: read per line as text
    because ``scripts/gate`` and the Makefile are shell and make -- a YAML-only scan would
    cover one of three ways a flag gets back in and call itself complete.
    """
    offences: list[str] = []
    for relative in ORDER_PINNING_CONFIGS:
        path = SERVICE_ROOT / relative
        assert path.is_file(), f"enforcement config vanished: {relative}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            tokens = line.split()
            for flag in _ORDER_PINNING_FLAGS:
                if any(flag in token for token in tokens):
                    offences.append(f"{relative}:{number}: {line.strip()}")
    assert not offences, (
        "a config that decides what the gates run pins the test order, so every "
        "run through it is one interleaving reported as a passing suite:\n" + "\n".join(offences)
    )
