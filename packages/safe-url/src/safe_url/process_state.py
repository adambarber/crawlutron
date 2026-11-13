"""Process-state registry: modules register a reset/probe pair.

The harness drives resets between tests so no state leaks across cases.
"""

from __future__ import annotations

from collections.abc import Callable

_Pairs: dict[str, tuple[Callable[[], None], Callable[[], bool]]] = {}


def register(owner: str, reset: Callable[[], None], has: Callable[[], bool]) -> None:
    """Register ``owner``'s reset/probe pair; re-registration replaces the old one.

    The probe must read what the reset empties: ``False`` over a populated store
    certifies a reset that failed.
    """
    _Pairs[owner] = (reset, has)


def reset_all_process_state() -> None:
    """Drop every registered owner's state, in registration order.

    One owner's failure never strands the rest: each reset runs, and the first
    failure is re-raised with its owner named once the chain completes (#643).
    """
    failing: tuple[str, BaseException] | None = None
    for name, (drop, _) in list(_Pairs.items()):
        try:
            drop()
        except BaseException as exc:
            if failing is None:
                failing = (name, exc)
    if failing is not None:
        who, err = failing
        raise RuntimeError(f"process-state reset failed for {who}") from err


def has_any_process_state() -> bool:
    """True when at least one registered owner still holds state."""
    return any(probe() for _, probe in _Pairs.values())


def unregister(owner: str) -> bool:
    """Remove one owner's registration; True when it was present.

    Test probes must remove their fake owners: a leftover registration
    pollutes ``registered_owners()`` and lies to the exhaustiveness invariants.
    """
    if owner in _Pairs:
        del _Pairs[owner]
        return True
    return False


def owner_has_state(owner: str) -> bool:
    """The probe of one registered owner, for tests asserting one module's state."""
    pair = _Pairs.get(owner)
    if pair is None:
        raise KeyError(f"{owner} never registered a process-state pair")
    return pair[1]()


def registered_owners() -> tuple[str, ...]:
    """Every registered owner's module name, in registration order."""
    return tuple(_Pairs)


__all__ = [
    "has_any_process_state",
    "owner_has_state",
    "register",
    "registered_owners",
    "reset_all_process_state",
    "unregister",
]
