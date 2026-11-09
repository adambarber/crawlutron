"""Unit tests for :class:`crawlutron.api.caller_concurrency.CallerConcurrencyTracker`."""

from __future__ import annotations

import asyncio

from crawlutron.api.caller_concurrency import CallerConcurrencyTracker


class TestCallerConcurrencyTrackerLocks:
    """#665: per-caller slot locks persist for the tracker's lifetime.

    The admit path serializes count->insert through the caller's lock; a
    release must never evict it, or a concurrent admit that already fetched
    the lock ends up serialized against a DIFFERENT lock than the next admit.
    """

    def test_lock_identity_is_stable_across_release_to_zero(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        lock = tracker.slot_lock(caller)
        tracker.reserve(caller)
        tracker.release(caller)
        assert tracker.slot_lock(caller) is lock

    def test_lock_persists_while_in_flight(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        tracker.slot_lock(caller)
        tracker.reserve(caller)
        tracker.reserve(caller)
        tracker.release(caller)  # count drops to 1
        assert tracker.sync_in_flight(caller) == 1
        assert tracker.has_slot_lock(caller)

    def test_release_to_zero_keeps_lock_registered(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        tracker.slot_lock(caller)
        tracker.reserve(caller)
        tracker.release(caller)
        assert tracker.has_slot_lock(caller)


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
            tracker.reserve(caller)
            lock_b = tracker.slot_lock(caller)  # B's admit path fetched the lock
            # B is preempted before entering. A completes: release to zero.
            tracker.release(caller)
            async with lock_b:  # B enters the (unregistered) lock: its admit window
                lock_c = tracker.slot_lock(caller)  # C arrives
                return lock_c is lock_b

        assert asyncio.run(interleave())
