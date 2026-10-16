from collections.abc import Iterator
from contextlib import contextmanager

from app.transport.browser import BrowserRenderer
from app.transport.browser_pool import (
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS,
    BrowserPool,
    BrowserPoolExhaustedError,
    set_browser_pool,
)


@contextmanager
def owned_browser_pool(pool: BrowserPool | None) -> Iterator[None]:
    """Install ``pool`` as the process-wide browser pool, then restore the previous
    one, so no later test's 503-versus-200 answer depends on ordering; ``None``
    installs the no-pool state that asserts the pool-down guard answers 503.
    """
    previous = set_browser_pool(pool)
    try:
        yield
    finally:
        set_browser_pool(previous)


class FakeBrowserPool(BrowserPool):
    """A browser pool that satisfies the engine's guard and refuses to render, by
    subclassing ``BrowserPool`` (whose no-I/O ``__init__`` is cheap) and raising in
    ``acquire`` -- a browser that renders during a hermetic test is a failure.
    """

    def __init__(self) -> None:
        super().__init__(pool_size=1)
        self.closed = False

    async def acquire(self, timeout: float | None = BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS) -> BrowserRenderer:
        raise AssertionError("a test that installs a fake pool must never acquire a renderer")

    async def close(self) -> None:
        self.closed = True
        await super().close()


class RaisingRenderer:
    """A renderer whose ``render`` raises: the worker's failure path under test."""

    async def render(self, url, options, *, policy=None):
        raise RuntimeError("renderer died")


class NoRendererPool(BrowserPool):
    """A pool that hands out no real renderer; every failure-path double overrides
    only ``acquire``, so the ``__init__``/``release``/``close`` trio lives here and
    subclassing hands the worker something its own constructor accepts.
    """

    def __init__(self) -> None:
        super().__init__(pool_size=1)

    def release(self, renderer) -> None:
        pass

    async def close(self) -> None:
        pass


class ExhaustedPool(NoRendererPool):
    """A real pool with no renderer: ``acquire`` always raises.

    ``BrowserPool.__init__`` does no I/O, so a real instance is as cheap as a stub,
    and every method the worker does not reach stays the production one.
    """

    async def acquire(self, timeout: float | None = None) -> BrowserRenderer:
        raise BrowserPoolExhaustedError("no renderer")


class RaisingRendererPool(NoRendererPool):
    """A pool that hands out :class:`RaisingRenderer`.

    The pool half of the worker's failure path: ``acquire`` succeeds and the render
    blows up, so any classified result has to come from the worker, not the guard.
    """

    async def acquire(self, timeout: float | None = None) -> BrowserRenderer:
        return RaisingRenderer()
