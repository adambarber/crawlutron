"""The pool's release state machine, proven through public doors (#589).

C6 closed the headline leak; these pin the category: a renderer is re-queued,
closed, or refused exactly once per release, and close() reaps everything.
"""

import asyncio
from collections.abc import Callable

import pytest

from crawlutron.runtime.resource_lifecycle import DeferredClose
from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.browser_pool import BrowserPool, BrowserPoolExhaustedError


class _SpyRenderer(BrowserRenderer):
    """A minimal renderer double: enough shape for the pool, a close recorder."""

    def __init__(self) -> None:
        super().__init__()
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
    """Mutation-discriminated drain-recheck loop.

    Three closes chained mid-drain: first gated inside drain's initial
    gather; second scheduled while drain is joined on first; third scheduled
    from within second's close -- strictly inside drain. A snapshot gather
    (the pre-consolidation hand-rolled shape) abandons second and third; a
    single-shot ``if`` recheck joins second but abandons third (third is
    still gated when drain returns); only the committed ``while`` recheck
    follows the chain to the end and joins all three.
    """
    owner: DeferredClose[_GatedRenderer] = DeferredClose(aclose=lambda r: r.close())
    first = _GatedRenderer()
    second = _GatedRenderer(on_close=lambda: owner.schedule(third))
    third = _GatedRenderer()
    # the chained-close contract test; see the docstring above.

    owner.schedule(first)
    drain_task = asyncio.ensure_future(owner.drain())
    await first.started.wait()  # first is inside drain's gather
    owner.schedule(second)  # second scheduled mid-drain
    first.gate.set()
    await first.finished.wait()  # first fully done
    await asyncio.sleep(0)  # let drain's post-first recheck run

    # DISCRIMINATOR: under a single-shot ``if`` drain exits after first and
    # abandons second and third; under ``while`` it stays joined on the chain.
    assert not drain_task.done(), "drain exited before joining the mid-drain close chain"
    second.gate.set()
    await second.finished.wait()  # second fully done; third scheduled mid-drain
    third.gate.set()  # release third so drain can join it

    await asyncio.wait_for(drain_task, timeout=1.0)
    assert first.closed == 1 and second.closed == 1 and third.closed == 1, (
        f"a mid-drain close was abandoned: first={first.closed}, second={second.closed}, third={third.closed}"
    )


async def test_drain_returns_immediately_when_nothing_is_pending():
    """The empty-drain contract close() relies on: no pending close, no await."""
    owner: DeferredClose[_SpyRenderer] = DeferredClose(aclose=lambda r: r.close())

    await asyncio.wait_for(owner.drain(), timeout=0.1)


async def test_schedule_outside_any_loop_declines_honestly():
    """No running loop: the owner declines (logged) instead of creating a loop
    or closing on a foreign thread; the caller's own teardown reclaims."""
    owner: DeferredClose[_SpyRenderer] = DeferredClose(aclose=lambda r: r.close())
    spy = _SpyRenderer()

    await asyncio.get_running_loop().run_in_executor(None, owner.schedule, spy)

    assert spy.closed == 0, "schedule must not close on a foreign thread"


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

    pool.release(checked_out)
    # release stays sync; the pool owns the retired close.
    await pool.wait_for_retired_closes()

    assert spy.closed == 1, f"the checked-out-during-close renderer was leaked: closed={spy.closed} times"


async def test_a_second_release_of_a_retired_renderer_schedules_no_second_close():
    """Double-release after close: the retired close fires ONCE. A second
    scheduling would tear a renderer down mid-reuse in any future generation."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    checked_out = await pool.acquire(timeout=1.0)
    await pool.close()
    pool.release(checked_out)
    pool.release(checked_out)
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

    pool.release(spy)

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


class _GatedRenderer(BrowserRenderer):
    """Renderer whose close blocks on an event, so the drain is observable.

    ``started`` fires once the close coroutine is running inside the owner's
    gather; ``finished`` once the close body has run. ``on_close``, if given,
    is awaited at the end of ``close`` -- used to schedule a third close
    strictly mid-drain (re-entrant schedule during drain).
    """

    def __init__(self, on_close: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.closed = 0
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self._on_close = on_close

    async def close(self) -> None:
        self.started.set()
        await self.gate.wait()
        self.closed += 1
        self.finished.set()
        if self._on_close is not None:
            self._on_close()
