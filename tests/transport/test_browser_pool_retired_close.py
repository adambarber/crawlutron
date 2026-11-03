"""A renderer checked out during pool.close() must be CLOSED, not dropped (#C6).

close() tears down the renderers it holds and retires everything else; a
renderer checked out at that moment is dropped by release()'s retired path
with only a debug log -- its ~700 MiB browser process outlives the pool.
"""

from crawlutron.transport.browser_pool import BrowserPool


class _SpyRenderer:
    """A minimal renderer double: enough shape for the pool, a close recorder."""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_release_closes_a_renderer_retired_by_close():
    """Checkout, then close the pool mid-render, then release: the pool must not
    close the checked-out renderer mid-flight (close()'s per-renderer sweep
    races the active render and its failure is swallowed), and the RETIRED
    path -- release -- must await the close instead."""
    pool = BrowserPool(pool_size=1)
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
