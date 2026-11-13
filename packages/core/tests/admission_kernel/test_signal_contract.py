"""Signal seam contract: edge-triggered wake semantics."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests._support._gate_factory import FakeSignal, ThreadSafeSignal


@pytest.mark.parametrize("signal", [FakeSignal(), ThreadSafeSignal()], ids=["fake", "thread-safe"])
async def test_signal_wakes_each_waiter_exactly_once_per_set(signal) -> None:
    woke = 0

    async def _counting_waiter() -> None:
        nonlocal woke
        await signal.wait(None)
        woke += 1

    w = asyncio.create_task(_counting_waiter())
    await asyncio.sleep(0)  # let the waiter reach its suspension point
    assert not w.done()  # parked, not yet woken
    signal.set()
    await w
    assert woke == 1

    # Re-park with no intervening set: no spurious wake.
    woke = 0
    w = asyncio.create_task(_counting_waiter())
    await asyncio.sleep(0)
    assert woke == 0

    signal.set()
    await w
    assert woke == 1


@pytest.mark.parametrize("signal", [FakeSignal(), ThreadSafeSignal()], ids=["fake", "thread-safe"])
async def test_waiter_after_consumed_set_does_not_inherit_stale_latch(signal) -> None:
    signal.set()  # set with no waiter: the latch is set
    w = asyncio.create_task(signal.wait(None))
    await asyncio.sleep(0)
    assert w.done() and w.result() is True  # first waiter consumed the latch once

    # A waiter arriving after the latch was consumed must not inherit a
    # permanently-set latch: it suspends until a fresh set().
    woke = False

    async def _late_waiter() -> None:
        nonlocal woke
        await signal.wait(None)
        woke = True

    w2 = asyncio.create_task(_late_waiter())
    await asyncio.sleep(0)
    assert not w2.done()  # suspended: no stale latch inherited
    signal.set()
    await w2
    assert woke


async def test_cross_thread_set_wakes_parked_waiter() -> None:
    signal = ThreadSafeSignal()
    woke = False

    async def _waiter_task() -> None:
        nonlocal woke
        await signal.wait(None)
        woke = True

    w = asyncio.create_task(_waiter_task())
    await asyncio.sleep(0)  # waiter parked; loop captured by the signal

    set_done = False

    def _set_from_thread() -> None:
        nonlocal set_done
        signal.set()
        set_done = True

    thread = threading.Thread(target=_set_from_thread)
    thread.start()
    thread.join(timeout=5.0)
    assert set_done, "cross-thread set() was not called"

    await asyncio.wait_for(w, timeout=5.0)  # the loop delivers the cross-thread set
    assert woke, "cross-thread set() did not wake the parked waiter"
