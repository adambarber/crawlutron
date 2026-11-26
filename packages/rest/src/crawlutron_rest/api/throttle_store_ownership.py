"""Ownership of Throttle instances holding a shared store (#1123 extraction)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from crawlutron.runtime import process_state

if TYPE_CHECKING:
    from crawlutron_rest.api.throttle import Throttle

__all__ = ["admit_bound", "drop_bound"]

_BOUND: set[Throttle] = set()
_BOUND_LOCK = threading.RLock()
_BOUND_SHARED_DB_OWNER = "crawlutron_rest.api.throttle-shared-db"


def admit_bound(throttle: Throttle) -> None:
    """Track one Throttle as holding a live shared store."""
    with _BOUND_LOCK:
        _BOUND.add(throttle)


def drop_bound(throttle: Throttle) -> None:
    """Stop tracking one Throttle: its store is closed or replaced."""
    with _BOUND_LOCK:
        _BOUND.discard(throttle)


def _close_bound_stores() -> None:
    """Close every shared store still held, through the owning Throttle's door."""
    with _BOUND_LOCK:
        bound = list(_BOUND)
        # Clear before unbinding: a Throttle admitted mid-sweep must stay owned.
        _BOUND.clear()
    for throttle in bound:
        throttle.unbind_shared_db()


def _has_bound_stores() -> bool:
    """Whether any Throttle still holds a shared store right now."""
    with _BOUND_LOCK:
        return bool(_BOUND)


process_state.register(_BOUND_SHARED_DB_OWNER, _close_bound_stores, _has_bound_stores)
