"""Tests run in a random order, always, and the enforcement is mechanical: the suite
passed in its written order until asked for another, so randomising is enforced from
both sides -- the live run and the gate configs -- because an ad-hoc opt-out is invisible to either alone.
"""

from __future__ import annotations

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

#: The ways a run can be pinned to a fixed order.
_ORDER_PINNING_FLAGS = ("no:randomly", "randomly-dont-reorganize", "randomly-dont-reset-seed")


def _seed_pinned_by_argv(argv) -> str | None:
    """Return the pinning token if argv pins the order via the seed sentinel."""
    for index, token in enumerate(argv):
        if token == "--randomly-seed=last":
            return token
        if token == "--randomly-seed" and index + 1 < len(argv) and argv[index + 1] == "last":
            return "--randomly-seed last"
    return None


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
    pinned = _seed_pinned_by_argv(pytestconfig.invocation_params.args)
    if pinned is not None:
        pytest.fail(
            f"this run pinned its order via {pinned!r}: the seed is replayed, not "
            "fresh, so the order did not vary. Repro runs deselect this test by "
            "NAME, never with a last-seed flag."
        )


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
