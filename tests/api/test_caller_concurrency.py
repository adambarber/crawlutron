"""Unit tests for :class:`app.api.caller_concurrency.CallerConcurrencyTracker`."""

from __future__ import annotations

import asyncio

from app.api.caller_concurrency import CallerConcurrencyTracker


class TestCallerConcurrencyTrackerEviction:
    """#511: the per-caller lock dict must not leak; locks for idle callers
    are evicted on release."""

    def test_evicts_lock_on_release_to_zero(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        # Real usage: slot_lock() called first (admit), then reserve()
        tracker.slot_lock(caller)
        tracker.reserve(caller)
        assert caller in tracker._slot_locks
        tracker.release(caller)
        # #511: a caller at zero with an idle lock must not leave the lock behind
        assert caller not in tracker._slot_locks

    def test_keeps_lock_while_in_flight(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        tracker.slot_lock(caller)
        tracker.reserve(caller)
        lock = tracker.slot_lock(caller)
        assert not lock.locked()
        # The lock stays while count > 0
        assert caller in tracker._slot_locks
        tracker.release(caller)
        assert caller not in tracker._slot_locks

    def test_keeps_lock_when_still_in_flight(self):
        """A second reserve before release keeps the lock."""
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        tracker.slot_lock(caller)
        tracker.reserve(caller)
        tracker.reserve(caller)
        tracker.release(caller)  # count drops to 1
        assert caller in tracker._slot_locks
        assert tracker.sync_in_flight(caller) == 1

    def test_evicts_only_idle_lock(self):
        """An actively held lock is NOT evicted on release; it evicts when
        release runs AFTER the lock is released (real usage pattern)."""
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        tracker.slot_lock(caller)
        tracker.reserve(caller)
        lock = tracker.slot_lock(caller)

        # Real usage: hold the lock for admit, then release it, then tracker.release()
        async def admit_then_release():
            async with lock:
                pass  # admit window
            # Lock is now released; tracker.release() runs outside the lock
            tracker.release(caller)
            # Lock is idle -> eviction must happen
            assert caller not in tracker._slot_locks

        asyncio.run(admit_then_release())


class TestCallerConcurrencyTrackerRace:
    """Concurrency-tracker helper behaviors."""

    def test_slot_lock_is_shared_across_reserves(self):
        tracker = CallerConcurrencyTracker()
        caller = "test-caller"
        lock1 = tracker.slot_lock(caller)
        lock2 = tracker.slot_lock(caller)
        assert lock1 is lock2
