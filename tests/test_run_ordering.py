"""Tests run in a random order, always, and the enforcement is mechanical.

WHY THIS EXISTS
---------------
This suite was written module by module, and the order tests were added in
became part of how they behaved. Tests passed in that order and were never
asked to pass in any other. A suite like that reports green for a reason that
has nothing to do with correctness: it is a measurement of one interleaving.

Randomising is the cheap half and it is not the hard half. The hard half is
that randomising the *default* is not enough, because a run can always opt back
out, and every opt-out is a request the reviewer has to notice. Two were in the
enforcement config itself — `-p no:randomly` on the pyright and safety-invariant
pre-commit hooks — which is the worst place for one: the hooks that exist to
catch a wrong order were themselves the runs that never varied. Both suites
were verified green under random ordering before the flags came off.

So the rule is enforced from both sides. :func:`test_this_run_was_randomized`
sees the run it is executing in, so an ad-hoc `pytest -p no:randomly` on a
developer's machine reports itself as a failure rather than as a passing run
that proved less than it looks. :func:`test_no_enforcement_config_pins_the_order`
reads the configs that decide what the gates run, so putting the flag back is
a failing test in the same commit that adds it.

WHAT EACH SIDE CANNOT SEE
-------------------------
The first test sees only the process it runs in: a flag added to a hook entry
is invisible to it, and the second sees only files, so a flag passed by hand on
a command line is invisible to it. Neither is sufficient alone. A gate that
only checked the config would be satisfied by a developer who has typed
`-p no:randomly` for six months, and one that only checked the live run would be
satisfied by a config that quietly stopped varying order in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The root both path derivations start from: this file is `tests/<name>.py`, so
# the service root is two parents up. Derived rather than imported so this gate
# keeps working if the helper module it would import is itself the thing that
# broke -- a gate whose liveness depends on the code it supervises cannot
# supervise it.
SERVICE_ROOT = Path(__file__).resolve().parent.parent

#: Configs that decide what the gates run, and so can decide that a run does
#: not vary. `pytest.ini` is the dangerous one: `addopts` is applied to every
#: invocation that does not override it, so a flag there is a silent default
#: rather than a visible command.
ORDER_PINNING_CONFIGS = (
    "pytest.ini",
    ".pre-commit-config.yaml",
    "Makefile",
    "scripts/gate",
    ".github/workflows/ci.yml",
)

#: The ways a run can be pinned to a fixed order. `-p no:randomly` unloads the
#: plugin outright; `--randomly-dont-reorganize` leaves it loaded and seeds it
#: but keeps the declared order, so it varies `random.seed()` while varying no
#: test ordering -- the subtler of the two, and the one a reader skims past.
#: Split on whitespace so `no:randomly` cannot be written to hide from `no:`.
_ORDER_PINNING_FLAGS = ("no:randomly", "randomly-dont-reorganize", "randomly-dont-reset-seed")


def test_this_run_was_randomized(pytestconfig: pytest.Config) -> None:
    """Fail unless the run in progress is actually varying test order.

    ``randomly_seed`` defaults to the string ``"default"`` when the plugin is
    loaded and is absent entirely when it is not, so an absent option is the
    signal that `-p no:randomly` was passed. ``randomly_reorganize`` is the
    second half: ``--randomly-dont-reorganize`` leaves the plugin loaded, so the
    seed reads as present while the declared order is kept.
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


def test_no_enforcement_config_pins_the_order() -> None:
    """Fail if a config that invokes the suite can pin the order it runs in.

    Read as text and matched per line rather than by importing YAML, because
    `scripts/gate` and the `Makefile` are shell and make, and a scan that only
    understood the YAML would cover one of the three ways a flag can get back in
    and report itself as complete.
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
