"""Per-domain pacing primitives for the scheduler: the token bucket and the
one remaining/nap pair every cooldown reader in this package shares (#580).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from secrets import SystemRandom

__all__ = [
    "COOLDOWN_PURGE_INTERVAL",
    "MAX_COOLDOWN_WAIT",
    "TokenBucket",
    "jittered_delay",
    "remaining_until",
    "sleep_out",
]

# Crawl-delay jitter draws from OS entropy, not Mersenne Twister: a predictable
# per-domain delay pattern is a fingerprint, and the syscall is free here.
_JITTER = SystemRandom()

# The jitter band. A fixed delay would make a fleet retry a host in lockstep; the
# band spreads the retries without changing the configured mean.
_JITTER_LOW, _JITTER_HIGH = 0.8, 1.2

# Upper bound on any single cooldown/nap sleep -- bounds batch patience.
MAX_COOLDOWN_WAIT = 300.0

# Purge the per-domain records at most once per this many cooldown sets, so a
# 429-heavy crawl does not rebuild the map per result. ``run`` purges up front.
COOLDOWN_PURGE_INTERVAL = 100


def jittered_delay(base_delay: float) -> float:
    """``base_delay`` with the crawl-delay jitter applied, or 0 for a zero base.
    Module-level so the jitter band lives in one place, tested directly."""
    if base_delay <= 0:
        return 0.0
    return base_delay * _JITTER.uniform(_JITTER_LOW, _JITTER_HIGH)


class TokenBucket:
    """Token bucket rate limiter: refills at ``rate`` tokens/sec, caps at ``capacity``."""

    def __init__(
        self, rate: float, capacity: int = 5, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self._clock = clock
        self.last_refill = clock()

    def consume(self, tokens: float = 1.0) -> float:
        """Reserve ``tokens``, returning seconds to wait before firing (0 if
        available). A short caller drives the balance negative, so overlapping
        callers queue behind what was taken (the max_rps herd)."""
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.rate)
        self.last_refill = now
        self.tokens -= tokens
        return max(0.0, -self.tokens / self.rate)


def remaining_until(expiry: float | None, now: float) -> float:
    """Seconds a stamped deadline still stands, capped; 0 when elapsed or absent.
    The ONE remaining-computation for every nap in this package (#580): each
    reader once compared its own clock family against the expiry."""
    if expiry is None:
        return 0.0
    return max(0.0, min(expiry - now, MAX_COOLDOWN_WAIT))


async def sleep_out(expiry: float | None, now: Callable[[], float]) -> None:
    """One capped nap on a standing deadline: the cap bounds the batch's
    patience; successive runs walk a window longer than the cap down."""
    remaining = remaining_until(expiry, now())
    if remaining > 0:
        await asyncio.sleep(remaining)
