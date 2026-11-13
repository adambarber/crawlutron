"""Process-state registry: modules register a reset/probe pair.

A behaviourally equivalent mirror of ``crawlutron.runtime.process_state``; safe_url owns its own reset seam.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = [
    "register",
    "unregister",
    "reset_all_process_state",
    "has_any_process_state",
    "owner_has_state",
    "registered_owners",
]

_Pairs: dict[str, tuple[Callable[[], None], Callable[[], bool]]] = {}
_Gate = threading.Lock()


def register(owner: str, reset: Callable[[], None], probe: Callable[[], bool]) -> None:
    """Store ``owner``'s reset/probe pair, replacing any earlier entry for it.

    The probe reports whether ``owner`` still holds state after a reset; a probe
    that says ``False`` over populated state certifies a failed reset.
    """
    with _Gate:
        _Pairs[owner] = (reset, probe)


def reset_all_process_state() -> None:
    """Run every registered reset, in registration order, then re-raise the first failure.

    A failing reset does not strand the later owners: every reset runs, and the
    first failure surfaces afterward, attributed to its owner (#643).
    """
    failing: tuple[str, BaseException] | None = None
    for owner, (drop, _) in list(_Pairs.items()):
        try:
            drop()
        except Exception as exc:
            if failing is None:
                failing = (owner, exc)
    if failing is not None:
        who, err = failing
        raise RuntimeError(f"process-state reset failed for {who}") from err


def has_any_process_state() -> bool:
    """True when at least one registered owner still holds state."""
    return any(probe() for _, probe in _Pairs.values())


def unregister(owner: str) -> bool:
    """Drop ``owner``'s registration; True when it was present."""
    with _Gate:
        if owner in _Pairs:
            del _Pairs[owner]
            return True
    return False


def owner_has_state(owner: str) -> bool:
    """Return ``owner``'s probe result, or raise when it never registered."""
    entry = _Pairs.get(owner)
    if entry is None:
        raise KeyError(f"{owner} never registered a process-state pair")
    return entry[1]()


def registered_owners() -> tuple[str, ...]:
    """The registered owner names, in registration order."""
    return tuple(_Pairs)
