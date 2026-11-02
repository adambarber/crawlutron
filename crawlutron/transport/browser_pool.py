"""Browser pool: manages N persistent BrowserRenderer instances.

Each renderer owns a Camoufox browser process (≈700 MiB reserved; ≈600–900 MiB RSS). The pool reuses these
across crawl requests so we don't spawn/destroy browsers per-site.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.browser_pool_exhausted import BrowserPoolExhaustedError
from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.taxonomy import CrawlStrategyName
from crawlutron.runtime.pool import AsyncPool
from crawlutron.runtime.pressure_sizing import (
    BROWSER_RAM_FRACTION,
)
from crawlutron.runtime.pressure_sizing import (
    derive_browser_pool_size as derive_browser_pool_size,
)
from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.results import crawl_result_from_exception
from crawlutron.vocabulary.strategies import BROWSER_HEADED_WARMED, BROWSER_HEADLESS

logger = logging.getLogger(__name__)


# Default bound for BrowserPool.acquire. Renders carry their own per-URL budget;
# this only stops a leaked renderer from hanging waiters with no diagnostics.
BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS: float = 60.0

# --- RAM-budget-aware browser pool sizing ---
# Each renderer holds ~600-900 MiB; sizing lives in derive_browser_pool_size.


class BrowserPool:
    """Pool of persistent BrowserRenderer instances.

    Provides acquire/release semantics so multiple concurrent crawl requests
    share a fixed set of browser processes.
    """

    def __init__(
        self,
        *,
        pool_size: int | None = None,
        settings: CrawlerSettings | None = None,
        warmup_urls: list[str] | None = None,
    ) -> None:
        self._pool_size = pool_size or derive_browser_pool_size()
        self._settings = settings
        self._warmup_urls = warmup_urls
        self._renderers: list[BrowserRenderer] = []
        self._available: asyncio.Queue[BrowserRenderer] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def pool_size(self) -> int:
        """The number of renderers this pool manages."""
        return self._pool_size

    async def initialize(self) -> None:
        """Create and warm up all browser renderers."""
        async with self._lock:
            if self._initialized:
                return
            for i in range(self._pool_size):
                # warmup_urls is the one renderer knob the pool forwards; declared
                # as its own typed parameter rather than an opaque kwargs bag.
                renderer = BrowserRenderer(settings=self._settings, warmup_urls=self._warmup_urls)
                self._renderers.append(renderer)
                await self._available.put(renderer)
                logger.info("browser_pool_renderer_created", extra={"index": i})
            self._initialized = True
            logger.info(
                "browser_pool_initialized",
                extra={"pool_size": self._pool_size, "ram_fraction": BROWSER_RAM_FRACTION},
            )

    async def acquire(self, timeout: float | None = BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS) -> BrowserRenderer:
        """Acquire a renderer from the pool; the wait is bounded so a leaked
        renderer shrinks capacity loudly rather than hanging every acquire with
        no diagnostics. Pass ``timeout=None`` only when the caller owns a deadline.
        """
        if not self._initialized:
            await self.initialize()
        try:
            if timeout is None:
                renderer = await self._available.get()
            else:
                renderer = await asyncio.wait_for(self._available.get(), timeout)
        except TimeoutError:
            available = self._available.qsize()
            logger.warning(
                "browser_pool_acquire_timeout",
                extra={"available": available, "pool_size": self._pool_size, "timeout_seconds": timeout},
            )
            raise BrowserPoolExhaustedError(
                f"No browser renderer available within {timeout}s "
                f"(available={available}, pool_size={self._pool_size}). "
                "The pool is exhausted or a renderer leaked; try again later."
            ) from None
        logger.debug("browser_pool_acquired", extra={"available": self._available.qsize()})
        return renderer

    def release(self, worker: BrowserRenderer) -> None:
        """Return a renderer to the pool, unless its generation was retired: one
        closed by ``close`` during checkout would otherwise re-enter the queue for
        the next acquire -- dropped here, the only place that can reintroduce it.
        """
        if not self._initialized or not any(r is worker for r in self._renderers):
            logger.debug(
                "browser_pool_release_retired",
                extra={"initialized": self._initialized, "available": self._available.qsize()},
            )
            return
        self._available.put_nowait(worker)
        logger.debug("browser_pool_released", extra={"available": self._available.qsize()})

    async def close(self) -> None:
        """Close all renderers and drain the idle queue, not just clear the list;
        keeping closed renderers queued made the pool's halves disagree -- a new
        generation queued behind closed ones, so early acquires returned them.
        """
        async with self._lock:
            for renderer in self._renderers:
                try:
                    await renderer.close()
                except Exception as e:
                    logger.warning("browser_pool_renderer_close_failed", extra={"error": str(e)})
            self._renderers.clear()
            while True:
                try:
                    self._available.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._initialized = False
            logger.info("browser_pool_closed", extra={"available": self._available.qsize()})

    def __len__(self) -> int:
        """How many renderers the pool holds (before init, none)."""
        return len(self._renderers)

    @property
    def browser_strategy(self) -> CrawlStrategyName:
        """The ``CrawlStrategyName`` label for this pool's homogeneous renders.

        The first renderer's profile stands for the pool (headed-warmed if it
        carries warmup URLs, else plain headless); before init, the kwargs decide.
        """
        warmup = self._warmup_urls
        if self._renderers:
            # The renderers resolve warmup URLs from the environment when the
            # constructor did not pass them, so the live list is authoritative.
            warmup = self._renderers[0].warmup_urls
        if warmup:
            return BROWSER_HEADED_WARMED
        return BROWSER_HEADLESS


if TYPE_CHECKING:
    # Static conformance, checked by mypy (the type gate): BrowserPool must satisfy
    # AsyncPool[BrowserRenderer]. A drift in acquire/release/__len__ fails the gate.
    _BROWSER_POOL_IS_AN_ASYNC_POOL: AsyncPool[BrowserRenderer] = BrowserPool(pool_size=1)


_BROWSER_STRATEGIES: frozenset[CrawlStrategyName] = frozenset({BROWSER_HEADLESS, BROWSER_HEADED_WARMED})


class PooledBrowserWorker:
    """Callable that acquires a renderer, renders, and releases -- the
    ``browser_worker`` callback for crawl and render requests. Its failure path
    returns the contracted ``CrawlResult`` shape rather than raising.
    """

    def __init__(self, pool: BrowserPool, *, strategy: CrawlStrategyName | None = None) -> None:
        self._pool = pool
        # Explicit when the caller names the chain entry; otherwise the pool's
        # renderer config supplies the mode. Validated here, not at use.
        if strategy is not None and strategy not in _BROWSER_STRATEGIES:
            raise ValueError(
                f"browser worker strategy must be one of {sorted(_BROWSER_STRATEGIES)}, got {strategy!r}"
            )
        self._strategy: CrawlStrategyName | None = strategy

    def _configured_strategy(self) -> CrawlStrategyName:
        if self._strategy is not None:
            return self._strategy
        # The pool publishes browser_strategy as the same CrawlStrategyName alias,
        # so the returned value is a declared member and nothing needs narrowing.
        return self._pool.browser_strategy

    async def __call__(self, job: CrawlJob) -> CrawlResult:
        started = time.monotonic()
        # The browser mode this pool actually renders with, resolved before any
        # attempt so the success and failure paths label the result identically.
        strategy = self._configured_strategy()
        try:
            renderer = await self._pool.acquire()
            try:
                result = await renderer.render(job.url, job.options, policy=job.host_policy)
                # The renderer does not know the pool's configured mode, so it
                # leaves the slot empty; fill it here, at the one boundary that does.
                if result.attempt.strategy is None:
                    result = replace(result, attempt=replace(result.attempt, strategy=strategy))
                return result
            finally:
                self._pool.release(renderer)
        except Exception as error:
            # Acquire must not escape: an exception would land in the scheduler's
            # blanket envelope and lose the re-queue, so return the contracted shape.
            return crawl_result_from_exception(
                error=error,
                url=job.url,
                domain=job.domain,
                strategy=strategy,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )


# Global pool instance (initialized at startup)
_browser_pool_singleton: BrowserPool | None = None


def get_browser_pool() -> BrowserPool | None:
    """Get the global browser pool (set up during lifespan), or None before it;
    never lazily constructs a divergent, wrongly-sized pool -- the size comes
    from settings at startup, not re-derived here.
    """
    return _browser_pool_singleton


def set_browser_pool(pool: BrowserPool | None) -> BrowserPool | None:
    """Set the global browser pool (for testing). Returns previous pool."""
    global _browser_pool_singleton
    old = _browser_pool_singleton
    _browser_pool_singleton = pool
    return old


async def initialize_browser_pool(settings: CrawlerSettings | None = None) -> BrowserPool:
    """Initialize the global browser pool with settings."""
    global _browser_pool_singleton
    _browser_pool_singleton = BrowserPool(settings=settings)
    await _browser_pool_singleton.initialize()
    return _browser_pool_singleton


async def close_browser_pool() -> None:
    """Close the global browser pool."""
    global _browser_pool_singleton
    if _browser_pool_singleton is not None:
        await _browser_pool_singleton.close()
        _browser_pool_singleton = None
