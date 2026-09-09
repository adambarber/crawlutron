"""Browser pool: manages N persistent BrowserRenderer instances.

Each renderer owns a Camoufox browser process (≈700 MiB reserved; ≈600–900 MiB RSS). The pool reuses these
across crawl requests so we don't spawn/destroy browsers per-site.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.crawl.strategies.browser import BrowserRenderer
from app.resource_pressure import (
    _BROWSER_RAM_FRACTION,
)
from app.resource_pressure import (
    derive_browser_pool_size as _derive_browser_pool_size,
)
from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


class BrowserPoolExhaustedError(Exception):
    """Raised when no renderer becomes available within the acquire timeout."""


# Default bound for BrowserPool.acquire (M12). Renders carry their own
# per-URL budget on top; this only stops a leaked renderer from hanging
# waiters forever with no diagnostics.
BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS: float = 60.0

# --- RAM-budget-aware browser pool sizing (adversarial perf: bounded browser pool) ---
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
                extra={"pool_size": self._pool_size, "ram_fraction": _BROWSER_RAM_FRACTION},
            )

    async def acquire(self, timeout: float | None = BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS) -> BrowserRenderer:
        """Acquire a renderer from the pool.

        The wait is bounded (M12): a leaked renderer must shrink capacity
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
        """Return a renderer to the pool."""
        self._available.put_nowait(renderer)
        logger.debug("browser_pool_released", extra={"available": self._available.qsize()})

    async def close(self) -> None:
        """Close all renderers and clean up."""
        async with self._lock:
            for renderer in self._renderers:
                try:
                    await renderer.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("browser_pool_renderer_close_failed", extra={"error": str(e)})
            self._renderers.clear()
            self._initialized = False
            logger.info("browser_pool_closed")


class PooledBrowserWorker:
    """Callable that acquires a renderer from the pool, renders, and releases.

    Used as the ``browser_worker`` callback in ``crawl_request``.
    """

    def __init__(self, pool: BrowserPool) -> None:
        self._pool = pool

    async def __call__(self, job: Any) -> Any:
        renderer = await self._pool.acquire()
        try:
            return await renderer.render(job.url, job.options)
        finally:
            self._pool.release(renderer)


# Global pool instance (initialized at startup)
_pool: BrowserPool | None = None


def get_browser_pool() -> BrowserPool | None:
    """Get the global browser pool (the one set up during lifespan).

    Returns None until ``initialize_browser_pool`` has run (or after
    ``close_browser_pool``), never lazily constructing a divergent,
    wrongly-sized pool -- the pool size is derived from settings at startup,
    not re-derived here. Callers that need the pool before startup (i.e. only
    browser strategies) must ensure startup ordering.
    """
    return _pool


def set_browser_pool(pool: BrowserPool | None) -> BrowserPool | None:
    """Set the global browser pool (for testing). Returns previous pool."""
    global _pool
    old = _pool
    _pool = pool
    return old


async def initialize_browser_pool(settings: CrawlerSettings | None = None) -> BrowserPool:
    """Initialize the global browser pool with settings."""
    global _pool
    _pool = BrowserPool(settings=settings)
    await _pool.initialize()
    return _pool


async def close_browser_pool() -> None:
    """Close the global browser pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
