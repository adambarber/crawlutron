"""The retention sweep owns garbage rows: tombstones past grace, orphaned
accumulators, expired results -- then truncates the WAL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from crawlutron.service.sweep_loop import sweep_forever
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.result_store import CrawlResultStore
from crawlutron.storage.safe_db import wal_checkpoint
from crawlutron.transport.freshness import default_clock

#: Tombstones are reclaimed 24 hours after deletion; younger ones survive.
TOMBSTONE_GRACE_SECONDS = 86400.0

#: One pass every five minutes, same cadence as the cache and spill sweeps.
RETENTION_SWEEP_INTERVAL_SECONDS = 300.0

__all__ = [
    "RETENTION_SWEEP_INTERVAL_SECONDS",
    "TOMBSTONE_GRACE_SECONDS",
    "retention_maintenance_once",
    "sweep_retention_forever",
]


def retention_maintenance_once(
    job_store: JobStore,
    result_store: CrawlResultStore,
    *,
    grace_seconds: float = TOMBSTONE_GRACE_SECONDS,
    now: float | None = None,
) -> int:
    """Reap every garbage row class in one pass; return rows reclaimed.

    Garbage: tombstones past grace, orphaned accumulators, expired results.
    """
    now = default_clock() if now is None else float(now)
    reclaimed = job_store.reap_tombstones(grace_seconds=grace_seconds, now=now)
    reclaimed += job_store.reap_orphan_accumulators()
    reclaimed += result_store.prune_expired(now=now)
    with job_store._lock:
        wal_checkpoint(job_store._conn)
    return reclaimed


async def sweep_retention_forever(
    job_store: JobStore,
    result_store: CrawlResultStore,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float = RETENTION_SWEEP_INTERVAL_SECONDS,
    grace_seconds: float = TOMBSTONE_GRACE_SECONDS,
    clock: Callable[[], float] = default_clock,
) -> None:
    """Run retention_maintenance_once on a bounded cadence until shutdown.

    A failing pass is logged; the loop continues. Store work runs off the loop.
    """

    async def tick() -> None:
        await asyncio.to_thread(
            retention_maintenance_once,
            job_store,
            result_store,
            grace_seconds=grace_seconds,
            now=clock(),
        )

    await sweep_forever(
        tick,
        shutdown_event,
        interval_seconds=interval_seconds,
        log_key="retention_maintenance_pass_failed",
    )
