"""Per-caller in-flight register for the sync door: sync crawls create no
job row to count concurrency with, so this in-memory per-caller counter is summed
with the async job-row count so one limit binds both doors."""

from __future__ import annotations

import asyncio
from collections import defaultdict


class CallerConcurrencyTracker:
    """Per-caller count of in-flight sync crawls, released to zero on completion.

    Also owns the per-caller admit lock that serializes the count->insert window.
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, int] = defaultdict(int)
        self._slot_locks: dict[str, asyncio.Lock] = {}

    def slot_lock(self, caller_id: str) -> asyncio.Lock:
        """The admit mutex for ``caller_id``; one per caller, lazily created.

        Locks persist for the tracker's lifetime (#665): bounded by the
        keyed-caller population; eviction/refcounting/sweep timers are
        refused as a second mechanism per owner ruling.
        """
        return self._slot_locks.setdefault(caller_id, asyncio.Lock())

    def reserve(self, caller_id: str) -> None:
        """Mark one more sync crawl in flight for ``caller_id``."""
        self._in_flight[caller_id] += 1

    def release(self, caller_id: str) -> None:
        """Mark one sync crawl done; a caller at zero is dropped, never clamped.

        The slot lock persists for the tracker's lifetime (#665): evicting it
        races a concurrent admit that already fetched the lock.
        """
        count = self._in_flight.get(caller_id, 0) - 1
        if count <= 0:
            self._in_flight.pop(caller_id, None)
        else:
            self._in_flight[caller_id] = count

    def sync_in_flight(self, caller_id: str) -> int:
        """How many sync crawls ``caller_id`` currently has running (0 when none)."""
        return self._in_flight.get(caller_id, 0)

    def has_slot_lock(self, caller_id: str) -> bool:
        """Whether ``caller_id``'s admit lock is still registered (the read door)."""
        return caller_id in self._slot_locks


__all__ = ["CallerConcurrencyTracker"]
