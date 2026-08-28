"""Browser pool: manages N persistent BrowserRenderer instances.

Each renderer owns a Camoufox browser process (~300MB). The pool reuses these
across crawl requests so we don't spawn/destroy browsers per-site.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from app.crawl.strategies.browser import BrowserRenderer
from app.settings import CrawlerSettings
from app.memory_pressure import read_memory_stats

logger = logging.getLogger(__name__)

# --- RAM-budget-aware browser pool sizing (adversarial perf: bounded browser pool) ---
# Each pooled renderer owns one persistent Camoufox process (~600-900 MiB RSS once
# WebExtensions/Web Content are counted). Because uvicorn runs one pool PER WORKER
# process, the only way to keep the TOTAL browser count bounded is a single worker
# (global pool) whose size is derived from the container's cgroup memory limit. The
# pool then reserves at most ``_BROWSER_RAM_FRACTION`` of RAM for browsers, leaving
# headroom for the cheap, high-volume HTTP path so the memory admission guard does
# not have to pause all dispatch to stay under the container ceiling.
_PER_BROWSER_RAM_BYTES = int(
    os.environ.get("BROWSER_CRAWLER_BROWSER_RAM_BYTES", str(700 * 1024 * 1024))
)
_BROWSER_RAM_FRACTION = float(os.environ.get("BROWSER_CRAWLER_BROWSER_RAM_FRACTION", "0.5"))
# Hard cap on the derived size (defense-in-depth against huge hosts / bad estimates).
_MAX_DERIVED_POOL_SIZE = int(os.environ.get("BROWSER_CRAWLER_MAX_DERIVED_POOL_SIZE", "4"))


def _derive_browser_pool_size() -> int:
    """Size the (single, global) browser pool to the container RAM budget.

    Honors an explicit ``BROWSER_CRAWLER_BROWSER_POOL_SIZE`` override; otherwise
    derives ``pool_size`` from the cgroup memory limit so the pool never consumes
    more than ``_BROWSER_RAM_FRACTION`` of RAM. Falls back to 1 when no limit is
    discoverable (bare-metal / unrestricted cgroup).
    """
    explicit = os.environ.get("BROWSER_CRAWLER_BROWSER_POOL_SIZE")
    if explicit:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            pass
    limit = read_memory_stats().total_bytes
    if limit and limit > 0:
        budget = int(limit * _BROWSER_RAM_FRACTION)
        size = max(1, budget // _PER_BROWSER_RAM_BYTES)
        return min(size, _MAX_DERIVED_POOL_SIZE)
    return 1


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

    async def acquire(self) -> BrowserRenderer:
        """Acquire a renderer from the pool (waits if all busy)."""
        if not self._initialized:
            await self.initialize()
        renderer = await self._available.get()
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
_pool: Optional[BrowserPool] = None


def get_browser_pool() -> BrowserPool | None:
    """Get the global browser pool (the one set up during lifespan).

    Returns None until ``initialize_browser_pool`` has run (or after
    ``close_browser_pool``), never lazily constructing a divergent,
    wrongly-sized pool -- the pool size is derived from settings at startup,
    not re-derived here. Callers that need the pool before startup (i.e. only
    browser strategies) must ensure startup ordering.
    """
    return _pool


def set_browser_pool(pool: Optional[BrowserPool]) -> Optional[BrowserPool]:
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
