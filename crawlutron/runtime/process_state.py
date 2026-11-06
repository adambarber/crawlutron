"""The one process-state seam (#621).

Modules holding state that outlives one call register a private reset/probe
pair here at import; the harness drives :func:`reset_all_process_state` instead
of importing a hand-copied ``reset_process_state`` from every owner.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_Entry = tuple[str, Callable[[], None], Callable[[], bool]]

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
    """Every registered module drops its state, in registration order."""
    for _, reset, _ in list(_ENTRIES):
        reset()


def has_any_process_state() -> bool:
    """Whether any registered owner still holds state."""
    return any(has() for _, _, has in list(_ENTRIES))


def owner_has_state(owner: str) -> bool:
    """The probe of one registered owner, for tests asserting one module's state."""
    for name, _, has in list(_ENTRIES):
        if name == owner:
            return has()
    raise KeyError(f"{owner} never registered a process-state pair")


def registered_owners() -> tuple[str, ...]:
    """Every registered owner's module name, in registration order."""
    return tuple(name for name, _, _ in list(_ENTRIES))
