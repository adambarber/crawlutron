"""BoundedTTLCache: one locked, TTL'd, LRU-bounded cache.

Choikepoints review, primitive #1. The same shape (a dict + a lock + a TTL
+ a cap) was hand-rolled at three sites and produced two lock bugs (the
robots cap mutating outside _ROBOTS_CACHE_LOCK; the DNS soft-cap clear
under the lock window). Here the lock lives INSIDE every operation, so
"forgot the lock" and "evicted outside the lock" are unrepresentable.

Value semantics: each entry stores (expires_at, value). Expired entries
read as absent. put() refreshes recency (LRU) and evicts
least-recently-used entries beyond max_entries.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class BoundedTTLCache(Generic[K, V]):
    """Locked + TTL + LRU-bounded in one audited place."""

    def __init__(self, max_entries: int, ttl: float, *, clock=time.monotonic) -> None:
        self._max_entries = max_entries
        self._ttl = ttl
        self._clock = clock
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def _entry_if_fresh(self, key: K, *, touch: bool, allow_stale: bool = False) -> V | None:
        """Shared read path: return the value if fresh, else None.

        Expired entries are NOT deleted here -- the engines' hand-rolled
        dicts leave expired entries in place (purge-only deletion), and the
        sitemap 304 path reads stale-but-present entries directly. Callers
        that want eager eviction use purge_expired().

        ``allow_stale`` is the 304-reuse contract, expressed as a flag on this
        one read path rather than as a second read path. It used to be a second
        path, and the cost of that is the two rules that MUST NOT differ between
        readers -- the expiry boundary and the LRU touch -- being declared twice,
        with nothing testing that they still agree. ``None`` and ``value`` are
        both legitimate returns here, so a divergence is invisible until a
        sitemap quietly stops seeing a last-known URL.
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
        """Fresh value for key, or None if absent/expired (LRU-touched).

        Expiry mirrors the engines' contract (``expires_at > now`` = fresh):
        the boundary instant itself is expired. Expired entries stay present
        (purge-only deletion) so stale-but-present reads keep working.
        """
        return self._entry_if_fresh(key, touch=True)

    def peek(self, key: K) -> V | None:
        """Value without the LRU touch (expiry still applies)."""
        return self._entry_if_fresh(key, touch=False)

    def get_stale(self, key: K) -> V | None:
        """Value even when expired (304-reuse contract), with LRU touch.

        The sitemap fetcher reuses last-known URLs when the origin answers
        304 Not Modified; the entry must be present, not fresh. Expiry is
        enforced by the fetch path (get), not here.
        """
        return self._entry_if_fresh(key, touch=True, allow_stale=True)

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
