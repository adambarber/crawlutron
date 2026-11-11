"""The resource lifecycle doubles: renderers that record or gate their closes."""

import asyncio
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import NoReturn, Protocol, cast

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


class SpyRenderer(BrowserRenderer):
    """A minimal renderer double: enough shape for the pool, a close recorder."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class GatedCloseRenderer(BrowserRenderer):
    """Renderer whose close blocks on an event, so the sweep is observable."""

    def __init__(self, name: str = "solo") -> None:
        super().__init__()
        self.name = name
        self.closed = 0
        self.gate = asyncio.Event()

    async def close(self) -> None:
        self.closed += 1
        await self.gate.wait()


class _GateOwner(Protocol):
    """Anything a shared #590 arrange can open: the gated close's event."""

    gate: asyncio.Event


def _as_gate_owner(worker: BrowserRenderer) -> _GateOwner:
    """Test doubles double as gate owners: constructed with a gate, handed to
    close_mid_sweep only after acquire handed them out as BrowserRenderer."""
    return cast(_GateOwner, worker)


async def close_mid_sweep(pool: BrowserPool, holder: BrowserRenderer) -> asyncio.Task[None]:
    """The shared #590 arrange: start close(), let it go mid-sweep holding the
    lock, release the holder into the sweep, then open the close gate.

    holder must own a gate -- that's true of every pool a test builds here
    (enforced by construction, not by the pool's type), so the cast below is
    provenance-documented, not assumed."""
    gated = _as_gate_owner(holder)
    close_task = asyncio.ensure_future(pool.close())
    await asyncio.sleep(0.01)  # close holds the lock, mid-sweep
    pool.release(holder)
    gated.gate.set()
    return close_task


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

    def harvest_timeout_for(self, browser_profile: Mapping[str, object] | None) -> float:
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


class FakeClient:
    def __init__(self):
        self.is_closed = False
        self._bound_loop = None

    async def aclose(self):
        self.is_closed = True


def bind_to_running_loop(client) -> None:
    """Bind a fake client to the running loop, or leave it unbound if there is none.

    An unbound fake is how the pool's "closed from the loop it was made on"
    question gets answered honestly."""
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None


class FakeWS:
    """A stand-in for Playwright's ``WebSocketRoute``: ``url``/``close()``/``connect_to_server()``."""

    def __init__(self, url):
        self.url = url
        self.closed_count = 0
        self.connect_calls = 0

    async def close(self, **kwargs):
        self.closed_count += 1

    def connect_to_server(self):
        self.connect_calls += 1
        return self
