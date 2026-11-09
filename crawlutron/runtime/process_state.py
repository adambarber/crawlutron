"""The one process-state seam (#621).

Modules holding state that outlives one call register a private reset/probe
pair here at import; the harness drives :func:`reset_all_process_state` instead
of importing a hand-copied ``reset_process_state`` from every owner. Scope:
stateful containers and singletons with observable behavior. Bounded pure-function
memo caches (``lru_cache`` on total functions) and in-flight registries emptied by
drain are not members: their entries are value-keyed and never go stale.
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
    """Every registered module drops its state, in registration order.

    One owner's failure never strands the rest: each reset runs, the first
    failure is re-raised with its owner named after the whole chain completes
    -- an aborted chain left later owners' state held into the next test (#643).
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


def owner_has_state(owner: str) -> bool:
    """The probe of one registered owner, for tests asserting one module's state."""
    for name, _, has in list(_ENTRIES):
        if name == owner:
            return has()
    raise KeyError(f"{owner} never registered a process-state pair")


def registered_owners() -> tuple[str, ...]:
    """Every registered owner's module name, in registration order."""
    return tuple(name for name, _, _ in list(_ENTRIES))
