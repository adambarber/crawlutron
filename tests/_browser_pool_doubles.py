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
    """Install ``pool`` as the process-wide browser pool, then restore the previous one.

    The save/restore lives here because a test that installs a pool and does not
    restore it makes every later test's 503-versus-200 answer depend on test
    ordering -- and the failure lands somewhere else entirely. Written once
    because the alternative is two copies that drift: the one that grows a
    ``try/finally`` protects its neighbours and the one that does not does not,
    and which is which is only visible in the diff.

    ``None`` installs the no-pool state, which is how a test asserts the pool-down
    guard answers 503.
    """
    previous = set_browser_pool(pool)
    try:
        yield
    finally:
        set_browser_pool(previous)


class FakeBrowserPool(BrowserPool):
    """A browser pool that satisfies the engine's guard and refuses to render.

    Subclasses ``BrowserPool`` rather than duck-typing it, so ``set_browser_pool``
    is checked against the class it actually stores: a stub that is not a pool
    can be handed to code calling any method on it, and the signature is the only
    thing that notices. ``BrowserPool.__init__`` does no I/O -- it only builds the
    queue and lock -- so a real instance is as cheap as a stub.

    ``acquire`` raises rather than returning a renderer. The tests that install a
    pool do it to stop the engine 503-ing with ``browser_pool_unavailable``, and a
    browser that renders during a hermetic test is a failure, not a slower test.
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
    """A pool that hands out no real renderer, with the pool half spelled once.

    Every failure-path double overrides ``acquire`` and nothing else, so the
    ``__init__``/``release``/``close`` trio is this class and each double
    contributes its one ``acquire``. Subclasses rather than duck-typing, so
    ``PooledBrowserWorker`` is handed something its own constructor accepts: a
    plain object with the two methods the failure path calls is not a
    ``BrowserPool``, and the annotation is the only thing that would notice.
    """

    def __init__(self) -> None:
        super().__init__(pool_size=1)

    def release(self, renderer) -> None:
        pass

    async def close(self) -> None:
        pass


class ExhaustedPool(NoRendererPool):
    """A real pool with no renderer: ``acquire`` always raises.

    ``BrowserPool.__init__`` does no I/O -- it only builds the queue and the
    lock -- so a real instance is as cheap as a stub, and every method the
    worker does NOT reach on this path stays the production one:
    ``browser_strategy`` is the inherited property, which already answers
    ``BROWSER_HEADLESS`` for a pool built with no warmup URLs.
    """

    async def acquire(self, timeout: float | None = None) -> BrowserRenderer:
        raise BrowserPoolExhaustedError("no renderer")


class RaisingRendererPool(NoRendererPool):
    """A pool that hands out :class:`RaisingRenderer`.

    The pool half of the worker's failure path: ``acquire`` succeeds and the
    render itself blows up, so a classified result has to come from the worker
    rather than from the pool guard.
    """

    async def acquire(self, timeout: float | None = None) -> BrowserRenderer:
        return RaisingRenderer()
