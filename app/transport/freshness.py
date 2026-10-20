"""One freshness voice for both caches: a TTL-to-expiry rule and a boundary.

ContentCache and HttpCache decided expiry in two places; both now route through this single rule.
"""

from __future__ import annotations

import time


def expiry_for(now: float, ttl: float) -> float | None:
    """Expiry for ``ttl`` at ``now``; ``None`` means never expires (``ttl <= 0``)."""
    return None if ttl <= 0 else now + ttl


def is_expired(now: float, expires: float | None) -> bool:
    """Whether an entry expiring at ``expires`` is stale at ``now``.

    ``None`` (never expires) is never stale; the boundary is ``expires <= now``."""
    return expires is not None and expires <= now


def default_clock() -> float:
    """The wall clock behind every TTL decision; injectable for tests."""
    return time.time()
