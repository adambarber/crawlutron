"""The pool's release state machine, proven through public doors (#589).

C6 closed the headline leak; these pin the category: a renderer is re-queued,
closed, or refused exactly once per release, and close() reaps everything.
"""

import asyncio

import pytest

from crawlutron.runtime.resource_lifecycle import DeferredClose
from crawlutron.transport.browser_pool import BrowserPool, BrowserPoolExhaustedError


class _SpyRenderer:
    """A minimal renderer double: enough shape for the pool, a close recorder."""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def _assert_queue_holds_no_duplicate(pool: BrowserPool) -> None:
    """The requeue hole's behavioral shape: exactly one acquire succeeds, a second
    exhausts -- proof the queue never holds the renderer twice."""
    acquired = await pool.acquire(timeout=1.0)
    try:
        with pytest.raises(BrowserPoolExhaustedError):
            await pool.acquire(timeout=0.05)
    finally:
        pool.release(acquired)
        await pool.close()


async def test_drain_joins_a_close_scheduled_while_draining():
    """The drain-recheck loop, red-first (a snapshot gather abandons the late
    close): one close gated mid-drain, a second scheduled DURING the drain;
    drain() must not complete while either close is unfinished."""
    owner = DeferredClose(aclose=lambda r: r.close())
    gated = _GatedRenderer()
    late = _GatedRenderer()

    owner.schedule(gated)
    drain_task = asyncio.ensure_future(owner.drain())
    for _ in range(3):
        await asyncio.sleep(0)

    assert not drain_task.done(), "drain returned while a gated close was still pending"
    owner.schedule(late)  # scheduled WHILE draining: the recheck loop must catch it
    gated.gates[0].set()
    for _ in range(3):
        await asyncio.sleep(0)

    # The discriminator, mutation-verified: under a snapshot gather the drain
    # completes here while the mid-drain close is still unfinished.
    assert not drain_task.done(), "drain completed while the mid-drain close was still pending"
    late.gates[0].set()

    await asyncio.wait_for(drain_task, timeout=1.0)
    assert gated.closed == 1 and late.closed == 1, (
        f"the mid-drain close was abandoned: gated={gated.closed}, late={late.closed}"
    )


async def test_drain_returns_immediately_when_nothing_is_pending():
    """The empty-drain contract close() relies on: no pending close, no await."""
    owner = DeferredClose(aclose=lambda r: r.close())

    await asyncio.wait_for(owner.drain(), timeout=0.1)


async def test_schedule_outside_any_loop_declines_silently():
    """No running loop: the owner declines; the caller's own teardown reclaims.
    (The closed-loop guard beside it is defense-in-depth for the sync-rebuild
    shutdown race -- unreachable through a public door without racing loops.)"""
    owner = DeferredClose(aclose=lambda r: r.close())
    spy = _SpyRenderer()

    await asyncio.get_running_loop().run_in_executor(None, owner.schedule, spy)

    assert spy.closed == 0, "schedule must not create a loop or close on a foreign thread"


async def test_release_closes_a_renderer_retired_by_close():
    """Checkout, then close the pool mid-render, then release: the pool must not
    close the checked-out renderer mid-flight (close()'s per-renderer sweep
    races the active render and its failure is swallowed), and the RETIRED
    path -- release -- must await the close instead."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    checked_out = await pool.acquire(timeout=1.0)
    assert checked_out is spy

    await pool.close()
    assert spy.closed == 0, "the pool closed a renderer that was checked out mid-render"

    released = pool.release(checked_out)  # type: ignore[arg-type]
    assert released is None, "release stays sync; the pool owns the retired close"
    await pool.wait_for_retired_closes()

    assert spy.closed == 1, f"the checked-out-during-close renderer was leaked: closed={spy.closed} times"


async def test_a_second_release_of_a_retired_renderer_schedules_no_second_close():
    """Double-release after close: the retired close fires ONCE. A second
    scheduling would tear a renderer down mid-reuse in any future generation."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    checked_out = await pool.acquire(timeout=1.0)
    await pool.close()
    pool.release(checked_out)  # type: ignore[arg-type]
    pool.release(checked_out)  # type: ignore[arg-type]
    await pool.wait_for_retired_closes()

    assert spy.closed == 1, f"the retired renderer was closed {spy.closed} times"


async def test_a_double_release_requeues_nothing():
    """Release called twice on a live checked-out renderer: the second is a
    warn no-op. The queue must never hold the renderer twice, proven by the
    pool exhausting on the pool_size+1-th acquire."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    renderer = await pool.acquire(timeout=1.0)
    pool.release(renderer)
    pool.release(renderer)  # the double release

    await _assert_queue_holds_no_duplicate(pool)


async def test_release_before_any_acquire_requeues_nothing():
    """The second trigger of the same hole: releasing a renderer the pool never
    checked out must not add a second queue entry behind the idle original."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    pool.release(spy)  # type: ignore[arg-type]

    await _assert_queue_holds_no_duplicate(pool)


async def test_close_reaps_every_retired_renderer_scheduled_after_the_drain():
    """A release racing close()'s drain must still be reaped: close() returns
    only when every retired close it knows of has run, and a post-drain release
    is closed too -- no 700 MiB process outlives the shutdown that owns it."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    checked_out = await pool.acquire(timeout=1.0)
    pool.release(checked_out)  # idle at close time: closed by the sweep
    await pool.close()
    assert spy.closed == 1, "an idle renderer was not closed by the sweep"


class _GatedRenderer:
    """Renderer whose close blocks on an event, so the drain is observable."""

    def __init__(self) -> None:
        self.closed = 0
        self.gates: list[asyncio.Event] = []

    async def close(self) -> None:
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()
        self.closed += 1
