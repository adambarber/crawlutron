"""#590 fix round: the release-during-sweep double close, and the invariant
that pins the single ownership chokepoint over renderer closes."""

import asyncio

from crawlutron.transport.browser_pool import BrowserPool
from tests._browser_doubles import GatedCloseRenderer, SpyRenderer, close_mid_sweep


async def test_release_during_sweep_await_window_never_double_closes():
    """Two renderers, both released, then one re-acquired so the queue order puts the idle one first in the
    sweep: a release landing while the sweep awaits that first close must not let the sweep reach the
    released one too -- the deferred close already owns it (#590).
    """
    first, second = GatedCloseRenderer("first"), GatedCloseRenderer("second")
    pool = BrowserPool.for_testing([first, second])

    a = await pool.acquire(timeout=1.0)
    b = await pool.acquire(timeout=1.0)
    # Release second then first, so the queue holds [second, first].
    pool.release(b)
    pool.release(a)
    re_acquired = await pool.acquire(timeout=None)  # takes `second`
    assert re_acquired is b
    assert a not in pool._checked_out, "first must be idle in the queue for the sweep to reach it"

    close_task = await close_mid_sweep(pool, re_acquired)
    first.gate.set()

    await asyncio.wait_for(close_task, timeout=2.0)
    assert second.closed == 1, (
        f"the released renderer was closed {second.closed} times: the sweep "
        "reached it after the deferred close already owned it"
    )
    assert first.closed == 1, first.closed


async def test_invariant_no_pool_path_closes_a_renderer_twice():
    """INVARIANT: release is exact over pool state."""
    for timing in (
        "before_close",
        "during_sweep",
        "after_close",
        "after_close_pending",
    ):
        spy = GatedCloseRenderer("solo")
        pool = BrowserPool.for_testing([spy])
        holder = await pool.acquire(timeout=1.0)
        close_task = asyncio.ensure_future(pool.close())
        if timing == "before_close":
            pool.release(holder)  # retired path: the sweep skips, release owes
        elif timing == "during_sweep":
            await asyncio.sleep(0.01)  # close() mid-run when the release lands
            pool.release(holder)
        elif timing == "after_close":
            pass
        else:
            # Ownership must hold while a deferred close is still pending:
            # close() returned, the gated close has not run, a release lands.
            spy.gate.set()
            await asyncio.wait_for(close_task, timeout=2.0)
            pool.release(holder)  # must be refused, not re-scheduled
        spy.gate.set()
        if timing != "after_close_pending":
            await asyncio.wait_for(close_task, timeout=2.0)
        pool.release(holder)  # any later release must close nothing
        await pool.wait_for_retired_closes()
        assert spy.closed == 1, f"[{timing}] closed {spy.closed} times"


async def test_double_release_during_sweep_closes_once():
    """#590: two releases of the SAME checked-out renderer while the sweep is blocked on another renderer's
    gated close -- release #1 takes the retired path, release #2 then schedules a SECOND close. The
    retired release must also discard ``_checked_out`` so release #2 hits the not-checked-out refusal.
    """
    target, blocker = GatedCloseRenderer("target"), GatedCloseRenderer("blocker")
    pool = BrowserPool.for_testing([target, blocker])

    a = await pool.acquire(timeout=1.0)
    b = await pool.acquire(timeout=1.0)
    assert a is target and b is blocker
    # Release the blocker first so the queue holds [blocker]: the sweep closes
    # it (gated) and BLOCKS mid-await -- that await is the sweep window.
    pool.release(b)
    close_task = asyncio.ensure_future(pool.close())
    # Wait (bounded, hang-safe) until the sweep parks the checked-out target
    # in _retired: a parking regression must FAIL the bound, not hang to 120s.

    async def wait_parked() -> None:
        while target not in pool._retired:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_parked(), timeout=2.0)
    except TimeoutError:
        raise AssertionError(
            "the sweep never parked the checked-out renderer in _retired; "
            "the double-release arrange could not be built"
        ) from None

    pool.release(target)  # #1: retired path -- schedules the close, owns it
    pool.release(target)  # #2: must NOT schedule a second close
    blocker.gate.set()
    target.gate.set()
    await asyncio.wait_for(close_task, timeout=2.0)
    await pool.wait_for_retired_closes()

    assert target.closed == 1, (
        f"target closed {target.closed} times: a double release during the "
        "sweep re-scheduled the close the retired release already owned"
    )
    assert blocker.closed == 1, blocker.closed


async def test_release_after_close_returns_still_closes_exactly_once():
    """A release after close() has fully returned schedules the retired close;
    joined via the same DeferredClose owner, the renderer closes once."""
    spy = SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    holder = await pool.acquire(timeout=1.0)
    await pool.close()
    pool.release(holder)
    await pool.wait_for_retired_closes()

    assert spy.closed == 1, spy.closed
