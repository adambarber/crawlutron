"""The one process-state seam (#621).

Modules holding state that outlives one call register a private reset/probe
pair here; the harness drives :func:`reset_all_process_state`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_Entry = tuple[str, Callable[[], None], Callable[[], bool]]

__all__ = [
    "has_any_process_state",
    "owner_has_state",
    "register",
    "registered_owners",
    "reset_all_process_state",
    "unregister",
]

_ENTRIES: list[_Entry] = []
_LOCK = threading.Lock()


def register(owner: str, reset: Callable[[], None], has: Callable[[], bool]) -> None:
    """Register ``owner``'s private reset/probe pair (re-registration replaces).

    The probe must read what the reset empties: a probe that says ``False`` over
    a populated store certifies a reset that failed.
    """
    with _LOCK:
        for index, (name, _, _) in enumerate(_ENTRIES):
            if name == owner:
                _ENTRIES[index] = (owner, reset, has)
                return
        _ENTRIES.append((owner, reset, has))


def reset_all_process_state() -> None:
    """Every registered module drops its state, in registration order.

    One owner's failure never strands the rest: each reset runs, and the first
    failure is re-raised with its owner named after the chain completes (#643).
    """
    first_failure: tuple[str, BaseException] | None = None
    for owner, reset, _ in list(_ENTRIES):
        try:
            reset()
        except Exception as e:
            if first_failure is None:
                first_failure = (owner, e)
    if first_failure is not None:
        failing_owner, error = first_failure
        raise RuntimeError(f"process-state reset failed for {failing_owner}") from error


def has_any_process_state() -> bool:
    """Whether any registered owner still holds state."""
    return any(has() for _, _, has in list(_ENTRIES))


def unregister(owner: str) -> bool:
    """Drop one owner's registration; True when it was present.

    Test probes must unregister their fake owners: a leftover registration
    pollutes ``registered_owners()`` and lies to the exhaustiveness invariants.
    """
    with _LOCK:
        for index, (name, _, _) in enumerate(_ENTRIES):
            if name == owner:
                del _ENTRIES[index]
                return True
    return False


def owner_has_state(owner: str) -> bool:
    """The probe of one registered owner, for tests asserting one module's state."""
    for name, _, has in list(_ENTRIES):
        if name == owner:
            return has()
    raise KeyError(f"{owner} never registered a process-state pair")


def registered_owners() -> tuple[str, ...]:
    """Every registered owner's module name, in registration order."""
    return tuple(name for name, _, _ in list(_ENTRIES))
