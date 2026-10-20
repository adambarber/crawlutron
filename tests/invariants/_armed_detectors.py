"""The hand-rolled detectors the arming audit must also sweep.
A ``Rule``-declared detector is armed because ``declared_rules`` walks its
``find=`` body; these sit outside every ``Rule(...)``, with their trigger-set proof."""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass

_INVARIANTS = pathlib.Path(__file__).resolve().parent


@dataclass(frozen=True)
class ArmedHandRolled:
    """A detector with no ``Rule(...)``: where it lives, and its trigger-set proof."""

    name: str
    module: pathlib.Path
    function: str
    premise: Callable[[], None]


def _observed(name: str) -> Callable[[], None]:
    """The registry observer's assertion: the live tree exercises this property."""
    from tests.property_registry import PROPERTY_BY_NAME

    prop = PROPERTY_BY_NAME[name]

    def premise() -> None:
        from tests.invariants.test_registry import _observed_property_count

        assert _observed_property_count(prop) > 0, (
            f"{name} detector observed no fingerprint occurrences on the live tree"
        )

    return premise


def _premises() -> dict[str, Callable[[], None]]:
    from tests.invariants import test_encoding_bounds, test_knob_contracts, test_public_surface

    def encoding() -> None:
        assert test_encoding_bounds._decodable_encoding_tokens(), "the decodable encoding set is empty"

    def knob() -> None:
        assert test_knob_contracts._consumed_knobs(), "the app reads no environment knob"

    def public_surface() -> None:
        assert test_public_surface._OWNER_PUBLIC_SURFACE_RATCHET, "the public-surface ratchet is empty"

    return {
        "undecodable-encoding": encoding,
        "resolved-env-reader": knob,
        "public-surface": public_surface,
        "bool-env": _observed("bool-env"),
        "robots-policy": _observed("robots-policy"),
        "public-target": _observed("public-target"),
    }


def hand_rolled_detectors() -> tuple[ArmedHandRolled, ...]:
    """Every detector in the suite that no ``Rule`` declares."""
    premises = _premises()
    return (
        ArmedHandRolled(
            name="undecodable-encoding",
            module=_INVARIANTS / "test_encoding_bounds.py",
            function="_undecodable_encoding_offenders",
            premise=premises["undecodable-encoding"],
        ),
        ArmedHandRolled(
            name="resolved-env-reader",
            module=_INVARIANTS / "test_knob_contracts.py",
            function="_resolved_env_reader_offenders",
            premise=premises["resolved-env-reader"],
        ),
        ArmedHandRolled(
            name="public-surface",
            module=_INVARIANTS / "test_public_surface.py",
            function="_public_surface_offenders",
            premise=premises["public-surface"],
        ),
        ArmedHandRolled(
            name="bool-env",
            module=_INVARIANTS / "test_registry_bool_env.py",
            function="_bool_env_offenders",
            premise=premises["bool-env"],
        ),
        ArmedHandRolled(
            name="robots-policy",
            module=_INVARIANTS / "test_registry_robots.py",
            function="_robots_policy_offenders",
            premise=premises["robots-policy"],
        ),
        ArmedHandRolled(
            name="public-target",
            module=_INVARIANTS / "test_registry_targets.py",
            function="_public_target_offenders",
            premise=premises["public-target"],
        ),
    )
