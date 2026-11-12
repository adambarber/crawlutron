"""The bounded-cadence sweep loop shared by every lifespan maintenance lane:
one fail-open forever-loop both the cache and spill sweeps ride.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = ["sweep_forever"]


async def sweep_forever(
    tick: Callable[[], Awaitable[object]],
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float,
    log_key: str,
) -> None:
    """Await ``tick`` on a bounded cadence until shutdown.

    A failing pass is logged and the loop continues (fail-open sweep law).
    """
    while not shutdown_event.is_set():
        try:
            await tick()
        except Exception:
            logger.exception(log_key)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
