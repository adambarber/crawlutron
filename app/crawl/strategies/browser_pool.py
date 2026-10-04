"""Browser pool: manages N persistent BrowserRenderer instances.

Each renderer owns a Camoufox browser process (≈700 MiB reserved; ≈600–900 MiB RSS). The pool reuses these
across crawl requests so we don't spawn/destroy browsers per-site.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from app.constants import BROWSER_HEADED_WARMED, BROWSER_HEADLESS
from app.crawl.result_helpers import crawl_result_from_exception
from app.crawl.strategies.browser import BrowserRenderer
from app.models.browser_pool_exhausted import BrowserPoolExhaustedError
from app.models.crawl_result import CrawlResult
from app.models.taxonomy import CrawlStrategyName
from app.resource_pressure import (
    BROWSER_RAM_FRACTION,
)
from app.resource_pressure import (
    derive_browser_pool_size as _derive_browser_pool_size,
)
from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


# Default bound for BrowserPool.acquire. Renders carry their own
# per-URL budget on top; this only stops a leaked renderer from hanging
# waiters forever with no diagnostics.
BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS: float = 60.0

# --- RAM-budget-aware browser pool sizing ---
# Each pooled renderer owns one persistent Camoufox process (~600-900 MiB RSS once
# WebExtensions/Web Content are counted). Because uvicorn runs one pool PER WORKER
# process, the only way to keep the TOTAL browser count bounded is a single worker
# Browser-pool RAM-budget sizing now lives in app.resource_pressure.derive_browser_pool_size
# (single source of truth shared with the httpx connection pool and the weighted admission).


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
        **renderer_kwargs: Any,
    ) -> None:
        self._pool_size = pool_size or _derive_browser_pool_size()
        self._settings = settings
        self._renderer_kwargs = renderer_kwargs
        self._renderers: list[BrowserRenderer] = []
        self._available: asyncio.Queue[BrowserRenderer] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Create and warm up all browser renderers."""
        async with self._lock:
            if self._initialized:
                return
            for i in range(self._pool_size):
                renderer = BrowserRenderer(settings=self._settings, **self._renderer_kwargs)
                self._renderers.append(renderer)
                await self._available.put(renderer)
                logger.info("browser_pool_renderer_created", extra={"index": i})
            self._initialized = True
            logger.info(
                "browser_pool_initialized",
                extra={"pool_size": self._pool_size, "ram_fraction": BROWSER_RAM_FRACTION},
            )

    async def acquire(self, timeout: float | None = BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS) -> BrowserRenderer:
        """Acquire a renderer from the pool.

        The wait is bounded: a leaked renderer must shrink capacity
        loudly, not hang every subsequent acquire forever with no
        diagnostics. Pass ``timeout=None`` only when the caller owns an
        outer deadline.
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

    def release(self, renderer: BrowserRenderer) -> None:
        """Return a renderer to the pool, unless its generation has been retired.

        A renderer that was checked out when ``close`` ran has already been
        closed by it, and returning it would put a closed renderer back in the
        queue for the next acquire to hand out. That is the same retired-
        generation state the queue drain in ``close`` prevents from the other
        side, so it is dropped here -- the only place that can reintroduce it.
        """
        if not self._initialized or not any(r is renderer for r in self._renderers):
            logger.debug(
                "browser_pool_release_retired",
                extra={"initialized": self._initialized, "available": self._available.qsize()},
            )
            return
        self._available.put_nowait(renderer)
        logger.debug("browser_pool_released", extra={"available": self._available.qsize()})

    async def close(self) -> None:
        """Close all renderers and clean up.

        The idle queue is drained as well as the renderer list cleared. Keeping
        the closed renderers queued made the pool's two halves disagree: the
        next ``initialize`` appended the new generation to the back of a queue
        that already held the old one, and ``acquire`` pops the front, so the
        first ``pool_size`` acquires after a recycle returned closed renderers
        and ``available`` reported double the real capacity.
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

    @property
    def browser_strategy(self) -> CrawlStrategyName:
        """The CrawlStrategy label for the renders this pool performs.

        A pool's renderers are homogeneous (all built from one kwargs set), so
        the first renderer's profile stands for the pool: the headed-warmed
        profile is the one carrying warmup URLs, every other configuration is a
        plain headless render. Before initialization the renderers do not exist
        yet, so the constructor kwargs decide, and those default to the
        headless profile. Exposed so a worker that fails before or during
        acquire can label its terminal result with the browser mode it was
        actually configured for, never an invented member.
        """
        warmup = self._renderer_kwargs.get("warmup_urls")
        if self._renderers:
            # The renderers resolve warmup URLs from the environment when the
            # constructor did not pass them, so the live list is authoritative.
            warmup = self._renderers[0].warmup_urls
        if warmup:
            return BROWSER_HEADED_WARMED
        return BROWSER_HEADLESS


_BROWSER_STRATEGIES: frozenset[CrawlStrategyName] = frozenset({BROWSER_HEADLESS, BROWSER_HEADED_WARMED})


class PooledBrowserWorker:
    """Callable that acquires a renderer from the pool, renders, and releases.

    Used as the ``browser_worker`` callback in ``crawl_request`` and
    ``render_request``. Its failure path returns the contracted
    ``CrawlResult`` shape (classified through the shared chokepoint) rather
    than raising: there is no catch site above it that can handle an
    ``EngineCrawlResult``-shaped substitute.
    """

    def __init__(self, pool: BrowserPool, *, strategy: CrawlStrategyName | None = None) -> None:
        self._pool = pool
        # Explicit when the caller knows which chain entry it is rendering for;
        # otherwise the pool's own renderer configuration supplies the real
        # browser mode. Never an invented strategy name.
        #
        # Validated HERE, not at use. `CrawlStrategyName` includes the two HTTP
        # strategies, so a browser worker could be built with
        # `HTTP_DECLARED_CRAWLER` and emit it as a browser result's
        # `selected_strategy`; a name outside the enum was accepted here and
        # rejected much later by `CrawlResult.__post_init__`. An annotation is
        # not a runtime check.
        if strategy is not None and strategy not in _BROWSER_STRATEGIES:
            raise ValueError(
                f"browser worker strategy must be one of {sorted(_BROWSER_STRATEGIES)}, got {strategy!r}"
            )
        self._strategy: CrawlStrategyName | None = strategy

    def _configured_strategy(self) -> CrawlStrategyName:
        if self._strategy is not None:
            return self._strategy
        # The pool publishes `browser_strategy` as the same `CrawlStrategyName`
        # alias this returns, so the value reaching the return is a declared
        # member and there is nothing here for a checker to narrow.
        #
        # This line carried a per-line pyright ignore (spelled without its
        # leading `#` here on purpose -- prose that quotes a suppression
        # directive IS one, which is how a comment in `app/api_models.py` had
        # been silently muting a line for as long as it stood there) calling the
        # finding a false positive, justified by a `reveal_type` probe of the
        # *property*. The probe was true and said nothing about this function.
        # What needed fixing was the constructor above, which accepted an HTTP
        # strategy name and only had it rejected much later by
        # `CrawlResult.__post_init__`; it validates there now, and the ignore has
        # had no finding to suppress since.
        return self._pool.browser_strategy

    async def __call__(self, job: Any) -> CrawlResult:
        started = time.monotonic()
        # The browser mode this pool actually renders with, resolved before any
        # attempt so the success and failure paths label the result identically.
        strategy = self._configured_strategy()
        try:
            renderer = await self._pool.acquire()
            try:
                result = await renderer.render(job.url, job.options, policy=job.host_policy)
                # The renderer does not know the pool's configured mode, so it
                # leaves the slot empty; fill it here, at the one boundary that
                # does, rather than letting the shaper guess a member. The slot
                # is on the attempt the result holds, which is the only place a
                # strategy is recorded now.
                if result.attempt.strategy is None:
                    result = replace(result, attempt=replace(result.attempt, strategy=strategy))
                return result
            finally:
                self._pool.release(renderer)
        except Exception as error:
            # Acquire (or, if a future renderer ever raises out of render,
            # that too) must not escape: the engine's browser consumer reads
            # result.metadata/elapsed_ms (absent on EngineCrawlResult) and the
            # render pipeline reads item["url"] (also absent), so an exception
            # here used to land in the scheduler's blanket envelope, get
            # misclassified as network_error, and lose the URL's at-least-once
            # re-queue. Return the contracted CrawlResult shape instead,
            # classified through the shared primitive so browser_pool_exhausted
            # with retryable=True survives to the frontier layer.
            return crawl_result_from_exception(
                error=error,
                url=job.url.raw,
                domain=job.domain,
                strategy=strategy,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )


# Global pool instance (initialized at startup)
_browser_pool_singleton: BrowserPool | None = None


def get_browser_pool() -> BrowserPool | None:
    """Get the global browser pool (the one set up during lifespan).

    Returns None until ``initialize_browser_pool`` has run (or after
    ``close_browser_pool``), never lazily constructing a divergent,
    wrongly-sized pool -- the pool size is derived from settings at startup,
    not re-derived here. Callers that need the pool before startup (i.e. only
    browser strategies) must ensure startup ordering.
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
