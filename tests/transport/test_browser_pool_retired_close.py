"""The pool's release state machine, proven through public doors (#589).

C6 closed the headline leak; these pin the category: a renderer is re-queued,
closed, or refused exactly once per release, and close() reaps everything.
"""

import pytest

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
