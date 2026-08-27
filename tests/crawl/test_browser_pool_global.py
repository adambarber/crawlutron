"""Global browser-pool access contract (L3).

The pool is a single module-level instance created during lifespan startup.
``get_browser_pool()`` must never lazily fabricate a divergent, wrongly-sized
pool (pool size is derived from settings at startup), and shutting down must
clear the global so a later access cannot hand out a closed pool.
"""

import pytest

from app.crawl.strategies.browser_pool import (
    get_browser_pool,
    set_browser_pool,
    close_browser_pool,
)


class _StubPool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _clear():
    set_browser_pool(None)


def test_get_browser_pool_returns_none_before_initialize():
    _clear()
    assert get_browser_pool() is None
    _clear()


def test_get_browser_pool_never_lazily_creates_divergent_pool():
    # Contract: calling getters before startup must NOT silently construct a
    # BrowserPool with settings-unaware default size. It must be None.
    _clear()
    assert get_browser_pool() is None
    _clear()


def test_get_browser_pool_returns_set_pool():
    stub = _StubPool()
    try:
        set_browser_pool(stub)
        assert get_browser_pool() is stub
    finally:
        _clear()


@pytest.mark.asyncio
async def test_close_browser_pool_nulls_global():
    stub = _StubPool()
    try:
        set_browser_pool(stub)
        await close_browser_pool()
        assert stub.closed
        assert get_browser_pool() is None
    finally:
        _clear()
