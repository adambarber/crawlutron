from collections.abc import Generator
from contextlib import contextmanager
from typing import NoReturn

from crawlutron.models.browser_profile_record import BrowserProfileRecord
from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.browser_driver import BrowserDriver
from crawlutron.transport.browser_pool import (
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS,
    BrowserPool,
    BrowserPoolExhaustedError,
    set_browser_pool,
)
from crawlutron.vocabulary.browser import BrowserAction


@contextmanager
def owned_browser_pool(pool: BrowserPool | None) -> Generator[None, None, None]:
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


class _MockDriver(BrowserDriver):
    """A minimal driver that does nothing -- used only to satisfy the type checker."""

    def __init__(self) -> None:
        # Skip the real BrowserDriver init which launches a browser
        self._browser = None
        self._context = None
        self._pages: list[object] = []

    async def close(self) -> None:
        pass

    def note_render(self) -> None:
        pass

    def harvest_timeout_for(self, browser_profile: BrowserProfileRecord | None) -> float:
        return 1.0

    async def safe_close_page(self, page: object) -> None:
        pass

    async def run_action_chain(
        self,
        page: object,
        actions: list[BrowserAction],
        *,
        url: str = "",
        budget_seconds: float | None = None,
    ) -> None:
        pass

    async def wait_for_ready_state(self, page: object) -> None:
        pass

    async def open_page(self, browser_profile: BrowserProfileRecord | None) -> NoReturn:
        raise AssertionError("RaisingRenderer should never open a page")


class RaisingRenderer(BrowserRenderer):
    """A renderer whose ``render`` raises: the worker's failure path under test."""

    def __init__(self) -> None:
        # Initialize with a mock driver to satisfy the type checker
        super().__init__(driver=_MockDriver())

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
