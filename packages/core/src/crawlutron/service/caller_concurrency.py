"""Per-caller in-flight sync-crawl register.

At ``CRAWLUTRON_WORKERS > 1`` the sync count lives in a coordination table
shared by every worker process so one quota binds across all workers.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from pathlib import Path

from crawlutron.service.errors import QuotaRefusal

__all__ = ["CallerConcurrencyTracker"]


class CallerConcurrencyTracker:
    """Per-caller in-flight sync count, released to zero on completion.

    Also owns the per-caller admit lock; when ``coordination_db_path`` is
    given, slot reservations are durable across all worker processes.
    """

    def __init__(
        self,
        *,
        coordination_db_path: str | Path | None = None,
    ) -> None:
        self._in_flight: dict[str, int] = defaultdict(int)
        self._slot_locks: dict[str, asyncio.Lock] = {}
        self._held: dict[str, list[str]] = {}
        self._coord: CoordinationHandle | None = None
        if coordination_db_path is not None:
            self._coord = CoordinationHandle(coordination_db_path)

    async def reserve(
        self,
        caller_id: str,
        *,
        max_concurrency: int | None = None,
        already_active: int = 0,
    ) -> None:
        """Reserve one sync slot; the coordination store enforces the quota."""
        if self._coord is None:
            self._in_flight[caller_id] += 1
            return
        if max_concurrency is None:
            raise ValueError("max_concurrency required when using coordination")
        slot_id = await self._coord.areserve(caller_id, max_concurrency, already_active)
        if slot_id is None:
            raise QuotaRefusal(
                f"Caller concurrency quota is {max_concurrency}, quota already filled",
            )
        self._held.setdefault(caller_id, []).append(slot_id)

    async def release(self, caller_id: str) -> None:
        """Release one sync slot for ``caller_id``."""
        if self._coord is not None:
            held = self._held.get(caller_id)
            if held:
                await self._coord.arelease(caller_id, held.pop())
            return
        count = self._in_flight.get(caller_id, 0) - 1
        if count <= 0:
            self._in_flight.pop(caller_id, None)
        else:
            self._in_flight[caller_id] = count

    async def sync_in_flight(self, caller_id: str) -> int:
        """How many sync crawls ``caller_id`` currently has running (0 when none).

        The coordination read runs on the store executor, never on the event
        loop; callers await it.
        """
        if self._coord is not None:
            return await self._coord.acount_active(caller_id)
        return self._in_flight.get(caller_id, 0)

    def slot_lock(self, caller_id: str) -> asyncio.Lock:
        """The admit mutex for ``caller_id``; persists for the tracker's lifetime (#665)."""
        return self._slot_locks.setdefault(caller_id, asyncio.Lock())

    def reset(self) -> None:
        """Clear admit locks, in-flight counts, held slots between tests."""
        # Registered with process_state so reset_all_process_state() drops stale
        # reservations. Clearing admit locks avoids stale event-loop binding (#665).
        self._slot_locks.clear()
        self._in_flight.clear()
        self._held.clear()

    def has_state(self) -> bool:
        """Whether the tracker holds any in-process reservation right now."""
        return bool(self._in_flight or self._held or self._slot_locks)

    def close(self) -> None:
        """Close the coordination store connection."""
        if self._coord is not None:
            self._coord.close()


class CoordinationHandle:
    """Thin wrapper around a ``CoordinationSyncSlotsStore`` instance."""

    __slots__ = ("_store",)

    def __init__(self, db_path: str | Path) -> None:
        from crawlutron.storage.coordination_sync_slots import (
            CoordinationSyncSlotsStore,
        )

        self._store = CoordinationSyncSlotsStore(db_path)

    async def areserve(self, caller_id: str, max_concurrency: int, already_active: int) -> str | None:
        """Reserve one slot at the current time; ``None`` refuses."""
        return await self._store.areserve(caller_id, max_concurrency, already_active, time.time())

    async def arelease(self, caller_id: str, slot_id: str) -> None:
        """Release one reserved slot."""
        await self._store.arelease(caller_id, slot_id)

    def count_active(self, caller_id: str) -> int:
        """Non-stale reserved slots for ``caller_id``."""
        return self._store.count_active(caller_id, time.time())

    async def acount_active(self, caller_id: str) -> int:
        """Non-stale reserved slots for ``caller_id``, awaited off the loop."""
        return await self._store.acount_active(caller_id, time.time())

    def close(self) -> None:
        self._store.close()
