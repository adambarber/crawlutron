"""BoundedTTLCache: one locked, TTL'd, LRU-bounded cache, standing in for the
hand-rolled dict+lock+TTL shapes that produced two lock bugs. The lock lives
inside every operation, so "forgot the lock" is unrepresentable.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class BoundedTTLCache[K, V]:
    """Locked + TTL + LRU-bounded in one audited place."""

    def __init__(self, max_entries: int, ttl: float, *, clock=time.monotonic) -> None:
        self._max_entries = max_entries
        self._ttl = ttl
        self._clock = clock
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def _entry_if_fresh(self, key: K, *, touch: bool, allow_stale: bool = False) -> V | None:
        """Shared read path: the value if fresh, else ``None``; expired entries stay
        in place (purge-only), so the sitemap 304 path reads stale-but-present
        ones. ``allow_stale`` keeps the 304 contract on this one read path.
        """
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now and not allow_stale:
                return None
            if touch:
                self._entries.move_to_end(key)
            return value

    def get(self, key: K) -> V | None:
        """Fresh value for key, or None if absent or expired (LRU-touched). Expiry
        mirrors the engines' contract -- ``expires_at > now`` is fresh, the
        boundary instant expired -- and expired entries stay present for stale reads.
        """
        return self._entry_if_fresh(key, touch=True)

    def peek(self, key: K) -> V | None:
        """Value without the LRU touch (expiry still applies)."""
        return self._entry_if_fresh(key, touch=False)

    def get_stale(self, key: K) -> V | None:
        """Value even when expired (the 304-reuse contract), LRU-touched: the
        sitemap fetcher reuses last-known URLs on ``304 Not Modified``, so the
        entry must be present, and expiry is enforced by the fetch path, not here.
        """
        return self._entry_if_fresh(key, touch=True, allow_stale=True)

    def _locked_entry(self, key: K) -> tuple[float, V] | None:
        """The stored (expires_at, value) under the lock, without a touch."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            return entry

    def time_until_expiry(self, key: K) -> float | None:
        """The entry's remaining TTL, or None when absent/expired.

        Lets a reader confirm a stored expiry without touching the entry
        table's layout.
        """
        entry = self._locked_entry(key)
        if entry is None:
            return None
        expires_at, _value = entry
        return expires_at - self._clock() if expires_at > self._clock() else None

    def put(self, key: K, value: V, *, ttl: float | None = None) -> None:
        """Insert/refresh under the lock, evicting LRU beyond max_entries."""
        now = self._clock()
        expires_at = now + (self._ttl if ttl is None else ttl)
        with self._lock:
            self._entries[key] = (expires_at, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry (test seams and reset paths)."""
        with self._lock:
            self._entries.clear()

    def discard(self, key: K) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def purge_expired(self, now: float | None = None) -> int:
        """Drop every expired entry; return the count removed.

        ``now`` is accepted (defaulting to the clock) so the engine's named
        purge seam stays deterministic in tests.
        """
        cutoff = self._clock() if now is None else now
        with self._lock:
            stale = [key for key, (expires_at, _v) in self._entries.items() if expires_at <= cutoff]
            for key in stale:
                del self._entries[key]
        return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
