"""M12: BrowserPool.acquire must not wait forever.

A leaked renderer (acquire without a matching release) would permanently
shrink capacity and hang every subsequent acquire with no diagnostics.
Acquires are now bounded: on timeout the pool raises
BrowserPoolExhaustedError carrying available/total counts.
"""

import inspect

from app.crawl.strategies.browser_pool import (
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS,
    BrowserPool,
    BrowserPoolExhaustedError,
)


class TestAcquireTimeout:
    async def test_acquire_times_out_with_diagnostics(self):
        pool = BrowserPool(pool_size=1)
        await pool.initialize()
        held = await pool.acquire()
        try:
            try:
                await pool.acquire(timeout=0.05)
            except BrowserPoolExhaustedError as error:
                message = str(error)
            else:
                raise AssertionError("second acquire should have timed out")
            assert "pool_size=1" in message
            assert "available=0" in message
        finally:
            pool.release(held)
        # After release the pool serves again.
        renderer = await pool.acquire(timeout=1.0)
        pool.release(renderer)

    def test_acquire_default_timeout_is_bounded(self):
        params = inspect.signature(BrowserPool.acquire).parameters
        default = params["timeout"].default
        assert default == BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS
        assert default is not None and default > 0
