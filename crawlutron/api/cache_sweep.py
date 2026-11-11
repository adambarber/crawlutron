"""The cache maintenance sweep: one pass prunes expired disk-cache envelopes
(and crash-orphaned .tmp files) from the shared content cache and purges the
expired robots rules, so reaping is not read-lazy only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from crawlutron.api.sweep_loop import sweep_forever
from crawlutron.orchestration.engine import purge_robots_cache
from crawlutron.transport.content_cache import ContentCache, shared_content_cache
from crawlutron.transport.freshness import default_clock

CACHE_MAINTENANCE_INTERVAL_SECONDS = 300.0

__all__ = ["CACHE_MAINTENANCE_INTERVAL_SECONDS", "cache_maintenance_once", "sweep_cache_forever"]


def cache_maintenance_once(cache: ContentCache | None = None, now: float | None = None) -> int:
    """Prune the shared content cache and expired robots rules once; return the
    removed count. ``cache``/``now`` are injected seams so the sweep reaps the
    directory production serves from.
    """
    removed = (cache if cache is not None else shared_content_cache()).prune()
    return removed + purge_robots_cache(now=now)


async def sweep_cache_forever(
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float = CACHE_MAINTENANCE_INTERVAL_SECONDS,
    clock: Callable[[], float] = default_clock,
) -> None:
    """Run ``cache_maintenance_once`` on a bounded cadence until shutdown.

    A failing pass is logged and the loop continues (fail-open sweep law).
    """

    async def tick() -> None:
        await asyncio.to_thread(cache_maintenance_once, now=clock())

    await sweep_forever(
        tick,
        shutdown_event,
        interval_seconds=interval_seconds,
        log_key="cache_maintenance_pass_failed",
    )
