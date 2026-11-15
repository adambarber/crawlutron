"""The store-write wait must not miss a write that already landed.

Level-triggered on the listener's generation: a landed write is observed instead
of being lost to a stale ``clear()``; the hang guard is preserved."""

from __future__ import annotations

import asyncio

import pytest

import tests._job_harnesses as _job_harnesses
from crawlutron.storage.safe_db import allow_on_loop
from tests._job_harnesses import _next_store_edge
from tests._job_store_doubles import _StoreListener, single_shot_job


class _SilentListener(_StoreListener):
    """A listener whose publish bumps the generation but never sets the event.

    Isolates the generation trigger from the event wake-up: a publish that landed
    must be observed even when no edge ever arrives.
    """

    def notify(self) -> None:
        with self._gen_lock:
            self._generation += 1


async def test_next_store_edge_returns_when_a_write_landed_before_the_wait():
    """A publish that completed before the waiter armed must not sleep the guard.

    Clear-then-wait wiped the earlier set() and blocked the full guard: a lost
    wake-up, not a wedge."""
    with single_shot_job() as (store, job):
        listener = store.status_bus.subscribe(job.job_id)
        try:
            with allow_on_loop():
                store.save(job)  # publishes: generation 0 -> 1, set() scheduled
            await asyncio.sleep(0)  # let the scheduled set() run
            await asyncio.wait_for(_next_store_edge(store, job.job_id, listener), timeout=5.0)
        finally:
            listener.close()


async def test_next_store_edge_observes_a_write_whose_edge_was_lost():
    """A publish that only advanced the generation is observed without waiting.

    The wait is level-triggered on the generation: bumping it without setting the
    event returns at once instead of sleeping on a silent edge."""
    with single_shot_job() as (store, job):
        listener = _SilentListener(store.status_bus, job.job_id, asyncio.get_running_loop())
        try:
            listener.notify()  # generation 0 -> 1, no event set
            await asyncio.wait_for(_next_store_edge(store, job.job_id, listener), timeout=5.0)
        finally:
            listener.close()


async def test_next_store_edge_still_fails_an_unwritten_store(monkeypatch):
    """A store never written still fails after the bound, same message shape.

    The bound is monkeypatched short so the test does not spend the real guard."""
    monkeypatch.setattr(_job_harnesses, "STORE_HANG_GUARD_SECONDS", 0.5)
    with single_shot_job() as (store, job):
        listener = store.status_bus.subscribe(job.job_id)
        try:
            with pytest.raises(AssertionError, match="no write reached job"):
                await _next_store_edge(store, job.job_id, listener)
        finally:
            listener.close()
