"""#590: close() racing an acquire suspended on the empty queue must never hand
the waiter a renderer the sweep closed -- the wake is re-validated under the
pool's lock.
"""

import asyncio

import pytest

from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.browser_pool import BrowserPool, BrowserPoolExhaustedError
from tests._browser_pool_doubles import SpyRenderer

_SpyRenderer = SpyRenderer


async def test_close_never_hands_a_suspended_acquire_a_swept_renderer():
    """A release requeues its renderer and wakes a suspended waiter; close()'s
    sweep must not close that renderer into the waiter's hands. The waiter's
    wake is re-checked under the pool lock, so it gets a clean refusal."""
    spy = _SpyRenderer()
    pool = BrowserPool.for_testing([spy])

    holder = await pool.acquire(timeout=1.0)
    waiter = asyncio.ensure_future(pool.acquire(timeout=1.0))
    await asyncio.sleep(0)  # the waiter is suspended on the now-empty queue
    pool.release(holder)  # requeues the renderer; the waiter's get() resolves
    await pool.close()  # the sweep must not close the requeued renderer into the waiter

    with pytest.raises(BrowserPoolExhaustedError):
        await waiter
    assert spy.closed == 1, "the requeued renderer was never reaped by the sweep"


class _GatedCloseRenderer(BrowserRenderer):
    """Renderer whose close blocks on an event, so the sweep is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0
        self.gate = asyncio.Event()

    async def close(self) -> None:
        self.closed += 1
        await self.gate.wait()


async def test_close_reaps_a_renderer_released_during_its_sweep():
    """A release landing while close's sweep is running must not enqueue the
    renderer into a pool being torn down, nor leak it: the closing flag routes
    the release to the retired close, which close's own drain joins."""
    spy = _GatedCloseRenderer()
    pool = BrowserPool.for_testing([spy])

    holder = await pool.acquire(timeout=1.0)
    close_task = asyncio.ensure_future(pool.close())
    await asyncio.sleep(0.01)  # close holds the lock, mid-sweep
    pool.release(holder)
    spy.gate.set()

    await close_task
    await pool.wait_for_retired_closes()
    assert spy.closed == 1, "a renderer released into a closing pool was leaked"
