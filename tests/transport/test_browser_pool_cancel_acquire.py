"""#1122: an acquire cancelled between the queue pop and the checkout must
return the renderer to the pool instead of shrinking it.
"""

import asyncio

import pytest

from crawlutron.transport.browser_pool import BrowserPool
from tests._browser_doubles import SpyRenderer


async def test_cancelled_acquire_requeues_the_popped_renderer():
    """Cancelling an acquire after it popped a renderer but before checkout
    must re-queue the renderer so the pool does not lose one."""
    spy = SpyRenderer()
    pool = BrowserPool.for_testing([spy])
    assert pool._available.qsize() == 1

    async with pool._lock:  # block the checkout registration
        task = asyncio.create_task(pool.acquire(timeout=1.0))
        # Wait until the renderer is popped and the acquire is blocked on the
        # checkout lock: that is the pop -> checkout hand-off window.
        while pool._available.qsize() != 0 or not pool._lock._waiters:
            await asyncio.sleep(0)
        assert pool._available.qsize() == 0
        assert spy not in pool._checked_out
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert pool._available.qsize() == 1, "the cancelled acquire did not re-queue the renderer"
    assert spy not in pool._checked_out

    fresh = await pool.acquire(timeout=1.0)
    assert fresh is spy
    pool.release(fresh)
