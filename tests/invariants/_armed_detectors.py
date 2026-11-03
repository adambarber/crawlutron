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
        # Observer walks register in _OBSERVERS at import; importing the detector
        # modules here guarantees the premise never audits an unregistered property.
        import tests.invariants.test_registry_bool_env  # noqa: F401
        import tests.invariants.test_registry_robots  # noqa: F401
        import tests.invariants.test_registry_targets  # noqa: F401
        from tests.invariants._registry_machinery import _observed_property_count

        assert _observed_property_count(prop) > 0, (
            f"{name} detector observed no fingerprint occurrences on the live tree"
        )

    return premise


def _subject_imports(module_path: pathlib.Path) -> Callable[[], None]:
    """Premise proving the detector's subject module exists on the live tree."""

    def premise() -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
        assert spec is not None and spec.loader is not None, f"{module_path} does not load"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod is not None

    return premise


def _premises() -> dict[str, Callable[[], None]]:
    from tests.invariants._encoding_sets import _ALWAYS_DECODABLE_ENCODINGS
    from tests.invariants._env_knobs import _consumed_knobs
    from tests.invariants._surface_ratchet import OWNER_PUBLIC_SURFACE_RATCHET

    def encoding() -> None:
        assert set(_ALWAYS_DECODABLE_ENCODINGS) | {"br"}, "the decodable encoding set is empty"

    def knob() -> None:
        assert _consumed_knobs(), "the app reads no environment knob"

    def public_surface() -> None:
        assert OWNER_PUBLIC_SURFACE_RATCHET, "the public-surface ratchet is empty"

    return {
        "undecodable-encoding": encoding,
        "resolved-env-reader": knob,
        "public-surface": public_surface,
        "bool-env": _observed("bool-env"),
        "robots-policy": _observed("robots-policy"),
        "public-target": _observed("public-target"),
    }


def _subject_detector(test_module_rel: str, source_rel: str, function: str) -> ArmedHandRolled:
    """ArmedHandRolled whose premise is that the subject source module loads."""
    return ArmedHandRolled(
        name=function,
        module=_INVARIANTS / test_module_rel,
        function=function,
        premise=_subject_imports(_INVARIANTS.parent.parent / source_rel),
    )


def hand_rolled_detectors() -> tuple[ArmedHandRolled, ...]:
    """Every detector in the suite that no ``Rule`` declares."""
    premises = _premises()
    base = [
        ArmedHandRolled(name=n, module=_INVARIANTS / m, function=f, premise=premises[p])
        for n, m, f, p in (
            (
                "undecodable-encoding",
                "test_encoding_bounds.py",
                "_undecodable_encoding_offenders",
                "undecodable-encoding",
            ),
            (
                "resolved-env-reader",
                "test_knob_contracts.py",
                "_resolved_env_reader_offenders",
                "resolved-env-reader",
            ),
            ("public-surface", "test_public_surface.py", "_public_surface_offenders", "public-surface"),
            ("bool-env", "test_registry_bool_env.py", "bool_env_offenders", "bool-env"),
            ("robots-policy", "test_registry_robots.py", "robots_policy_offenders", "robots-policy"),
            ("public-target", "test_registry_targets.py", "public_target_offenders", "public-target"),
        )
    ]
    chokepoint = [
        _subject_detector(
            "test_coordination_cookies_begin_lock.py",
            "crawlutron/storage/coordination_cookies.py",
            "_begin_immediate_inside_store_lock",
        ),
        _subject_detector(
            "test_atwin_store_executor.py",
            "crawlutron/storage/coordination_store.py",
            "_asyncio_to_thread_offenders",
        ),
        _subject_detector(
            "test_atwin_store_executor.py",
            "crawlutron/storage/coordination_store.py",
            "_atwin_executor_offenders",
        ),
        _subject_detector(
            "test_cookie_load_read_only.py",
            "crawlutron/storage/coordination_cookies.py",
            "_load_mutation_offenders",
        ),
        _subject_detector(
            "test_challenge_wait_poll.py", "crawlutron/transport/browser.py", "_raw_sleep_offenders"
        ),
        _subject_detector(
            "test_challenge_wait_poll.py", "crawlutron/transport/browser.py", "_too_many_extract_offenders"
        ),
    ]
    return (*base, *chokepoint)
