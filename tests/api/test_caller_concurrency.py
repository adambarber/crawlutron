"""Unit tests for :class:`crawlutron.service.caller_concurrency.CallerConcurrencyTracker`."""

from __future__ import annotations

import asyncio

from crawlutron.service.caller_concurrency import CallerConcurrencyTracker


def _tracker_with_lock(caller: str = "test-caller"):
    """One tracker with a slot lock already fetched; shared setup for lock-identity tests."""
    tracker = CallerConcurrencyTracker()
    lock = tracker.slot_lock(caller)
    return tracker, caller, lock


class TestCallerConcurrencyTrackerLocks:
    """#665: per-caller slot locks persist for the tracker's lifetime. The admit path serializes
    count->insert through the caller's lock; a release must never evict it, or a concurrent admit that
    already fetched the lock ends up serialized against a DIFFERENT lock than the next admit.
    """

    async def test_lock_identity_is_stable_across_release_to_zero(self):
        tracker, caller, lock = _tracker_with_lock()
        await tracker.reserve(caller)
        await tracker.release(caller)
        assert tracker.slot_lock(caller) is lock

    async def test_release_to_zero_keeps_lock_identity(self):
        tracker, caller, lock = _tracker_with_lock()
        await tracker.reserve(caller)
        await tracker.reserve(caller)
        await tracker.release(caller)  # count drops to 1
        assert await tracker.sync_in_flight(caller) == 1
        await tracker.release(caller)  # count drops to 0: the eviction trigger
        assert await tracker.sync_in_flight(caller) == 0
        assert tracker.slot_lock(caller) is lock


class TestCallerConcurrencyTrackerRace:
    """Concurrency-tracker helper behaviors."""

    def test_slot_lock_is_shared_across_reserves(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        lock1 = tracker.slot_lock(caller)
        lock2 = tracker.slot_lock(caller)
        assert lock1 is lock2

    def test_eviction_races_concurrent_admit_window(self):
        """The #665 interleaving: admit B fetched the lock but has not entered
        it yet; caller A's release-to-zero evicts it; admit C then mints a
        second lock and admits concurrently with B -- the budget undercounts."""

        async def interleave() -> bool:
            tracker = CallerConcurrencyTracker()
            caller = "test-caller"
            tracker.slot_lock(caller)
            await tracker.reserve(caller)
            lock_b = tracker.slot_lock(caller)  # B's admit path fetched the lock
            # B is preempted before entering. A completes: release to zero.
            await tracker.release(caller)
            async with lock_b:  # B enters the (unregistered) lock: its admit window
                lock_c = tracker.slot_lock(caller)  # C arrives
                return lock_c is lock_b

        assert asyncio.run(interleave())


class TestCallerConcurrencyTrackerOffLoop:
    """``sync_in_flight`` is awaited from the loop and reads the store's count."""

    async def test_sync_in_flight_returns_store_count_awaited_from_loop(self, tmp_path):
        """Awaited off the loop: red before the conversion (it was a sync int), green after."""
        tracker = CallerConcurrencyTracker(coordination_db_path=str(tmp_path / "coord.db"))
        await tracker.reserve("key-a", max_concurrency=10)
        await tracker.reserve("key-a", max_concurrency=10)
        assert await tracker.sync_in_flight("key-a") == 2
        assert await tracker.sync_in_flight("key-b") == 0
        await tracker.release("key-a")
        assert await tracker.sync_in_flight("key-a") == 1
