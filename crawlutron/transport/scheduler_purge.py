"""The scheduler's map purge: elapsed cooldowns/circuits/pins plus idle rate
state. Lives apart from scheduler.py so that class stays at its
348-line budget; the scheduler holds the returned maps wholesale."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawlutron.transport.scheduler import TokenBucket

__all__ = ["new_rate_and_pin_state", "purge_scheduler_maps"]

#: The six scheduler maps the purge reads and rewrites wholesale.
_Maps = tuple[
    dict[str, float],
    dict[str, float],
    dict[str, tuple[str, float]],
    dict[str, float],
    dict[str, float],
    "dict[str, TokenBucket]",
]


def new_rate_and_pin_state() -> tuple[dict[str, float], dict[str, float], dict[str, tuple[str, float]]]:
    """Fresh adaptive-latency, last-seen, and strategy-pin maps."""
    return {}, {}, {}


def purge_scheduler_maps(
    cooldowns: dict[str, float],
    circuits: dict[str, float],
    pins: dict[str, tuple[str, float]],
    emas: dict[str, float],
    last_seen: dict[str, float],
    buckets: dict[str, TokenBucket],
    pin_now: float,
    window: float,
    pin_ttl: float,
) -> _Maps:
    """Elapsed cooldowns/circuits, expired pins, and idle rate state, dropped
    in one pass; the scheduler keeps the returned maps wholesale."""
    now_wall = time.time()
    now_mono = time.monotonic()
    cooldowns = {d: exp for d, exp in cooldowns.items() if exp > now_wall}
    circuits = {d: exp for d, exp in circuits.items() if exp > now_mono}
    pins = {d: p for d, p in pins.items() if pin_now - p[1] <= pin_ttl}
    # EMAs decay by presence not expiry; idle buckets rebuild on demand.
    emas = {d: e for d, e in emas.items() if last_seen.get(d, pin_now) > pin_now - window}
    last_seen = {d: t for d, t in last_seen.items() if d in emas}
    live = set(cooldowns) | set(circuits) | set(emas)
    buckets = {d: b for d, b in buckets.items() if d in live}
    return cooldowns, circuits, pins, emas, last_seen, buckets
