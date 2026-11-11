"""The spill maintenance sweep: one pass runs the shared spill store's
fence-aware age GC, so ``crawl_spills`` rows do not grow without bound on a
long-running service. Same bounded-cadence shape as the cache sweep.
"""

from __future__ import annotations

import asyncio
import logging

from crawlutron.api.sweep_loop import sweep_forever
from crawlutron.storage.result_store import CrawlSpillStore

logger = logging.getLogger(__name__)

# Rows a released request owns are garbage once the result TTL window closes:
# the durable copy lives in ``results`` under its own TTL, so the spill copy
# never needs to outlive CRAWLUTRON_RESULT_TTL_SECONDS. The sweep cadence is
# the cache sweep's -- one maintenance lane, one tick.
SPILL_MAX_AGE_SECONDS = 3600.0
SPILL_SWEEP_INTERVAL_SECONDS = 300.0

__all__ = [
    "SPILL_MAX_AGE_SECONDS",
    "SPILL_SWEEP_INTERVAL_SECONDS",
    "spill_maintenance_once",
    "sweep_spills_forever",
]


async def spill_maintenance_once(
    store: CrawlSpillStore | None = None,
    *,
    max_age_seconds: float = SPILL_MAX_AGE_SECONDS,
) -> int:
    """Run the shared spill store's async age GC once; return rows reclaimed."""
    spill = store if store is not None else _shared_spill_store()
    return await spill.agc_stale_spills(max_age_seconds)


def _shared_spill_store() -> CrawlSpillStore:
    """The running app's shared spill store, for a sweep task wired without one."""
    from crawlutron.main import module_app

    app = module_app()
    store = getattr(app.state if app is not None else None, "spill_store", None)
    if store is None:
        raise RuntimeError("spill_maintenance_once needs a store: none wired in the module app")
    return store


async def sweep_spills_forever(
    store: CrawlSpillStore | None,
    *,
    shutdown_event: asyncio.Event,
    interval_seconds: float = SPILL_SWEEP_INTERVAL_SECONDS,
    max_age_seconds: float = SPILL_MAX_AGE_SECONDS,
) -> None:
    """Run ``spill_maintenance_once`` on a bounded cadence until shutdown.

    A failing pass is logged and the loop continues (fail-open sweep law).
    """
    if store is None:
        store = _shared_spill_store()
    await sweep_forever(
        lambda: spill_maintenance_once(store, max_age_seconds=max_age_seconds),
        shutdown_event,
        interval_seconds=interval_seconds,
        log_key="spill_maintenance_pass_failed",
    )
