"""In-container disk content cache (no sidecar / external service).

This complements :mod:`app.crawl.cache.HttpCache` (which stores only
``ETag``/``Last-Modified`` metadata for :rfc:`7232` conditional re-requests).
``ContentCache`` persists the actual fetched response on local disk so repeated
crawls of an unchanged URL avoid re-downloading (and, if callers choose to cache
extracted content, re-extracting) the page.

Design notes:
* Self-contained: only the Python stdlib (``json`` + ``hashlib`` + a file per
  entry). No Redis / Memcached sidecar required; mount a volume at the cache
  directory for cross-restart persistence.
* TTL via an injectable clock so tests stay deterministic without real sleeps.
* Thread-safe (one lock per instance) and JSON-enveloped so values are
  arbitrary JSON-serializable objects.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable


def _default_clock() -> float:
    return time.time()


class ContentCache:
    """Disk-backed key/value cache with per-entry TTL and optional LRU eviction."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        default_ttl: int = 3600,
        namespace: str = "content",
        clock: Callable[[], float] = _default_clock,
        max_entries: int = 10000,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.default_ttl = default_ttl
        self.namespace = namespace
        self.clock = clock
        self.max_entries = max_entries
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # In-memory LRU index (key -> path) so eviction never globs/stats the
        # whole cache directory on the hot path (#5). The on-disk file remains
        # the source of truth for values; this index only tracks recency/order.
        # (A warm restart with pre-existing files repopulates the index lazily
        # on first get/set of each key; prune() reconciles disk vs index.)
        self._index: "OrderedDict[str, Path]" = OrderedDict()
        # In-memory value cache: keep small values resident so repeated reads
        # of a hot URL never touch disk. Bounded LRU (max 100 entries) of
        # entries whose shallow size is < 1MB. The on-disk file remains the
        # source of truth; this is purely a read accelerator. Entries store a
        # (expires, value) tuple so get() can honor TTL without a disk read.
        self._memory_cache: "OrderedDict[str, tuple[float | None, Any]]" = OrderedDict()
        self._MEMORY_CACHE_MAX = 100
        self._MEMORY_CACHE_MAX_SIZE = 1024 * 1024

    # -- key handling -------------------------------------------------------
    @staticmethod
    def make_key(*parts: Any) -> str:
        """Build a stable cache key from arbitrary parts."""
        return "|".join(json.dumps(p, sort_keys=True, separators=(",", ":")) for p in parts)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{self.namespace}.{digest}.json"

    def _enforce_max_entries(self) -> None:
        """Evict least-recently-used entries using the in-memory index.

        Runs in O(overflow) unlinks with no directory glob/stat, unlike the
        previous implementation that listed and stat() every cache file on each
        write (#5).
        """
        if self.max_entries is None or self.max_entries <= 0:
            return
        with self._lock:
            while len(self._index) > self.max_entries:
                _key, old_path = self._index.popitem(last=False)
                try:
                    old_path.unlink()
                except OSError:
                    pass
                # Keep the memory value cache consistent with evicted disk keys.
                self._memory_cache.pop(_key, None)

    # -- public API ---------------------------------------------------------
    def _read_envelope(self, key: str) -> dict[str, Any] | None:
        """Return the unexpired envelope for ``key`` (registering it in the LRU
        index), or ``None`` if missing, expired, or unreadable.

        Shared by :meth:`get` and :meth:`cached_at_of` so the envelope-read +
        TTL-expiry logic lives in exactly one place.
        """
        path = self._path_for(key)
        with self._lock:
            if key in self._index:
                try:
                    envelope = json.loads(path.read_text())
                except (ValueError, OSError):
                    self._index.pop(key, None)
                    return None
            else:
                if not path.exists():
                    return None
                try:
                    envelope = json.loads(path.read_text())
                except (ValueError, OSError):
                    return None
                # Lazily register a cold on-disk entry into the index.
                self._index[key] = path
            expires = envelope.get("expires")
            if expires is not None and self.clock() > expires:
                try:
                    path.unlink()
                except OSError:
                    pass
                self._index.pop(key, None)
                return None
            # Mark recently used (LRU) by moving to the end of the ordered index.
            self._index.move_to_end(key)
            return envelope

    def get(self, key: str, ttl: int | None = None) -> Any | None:
        """Return the cached value if present and unexpired, else ``None``.

        Checks the in-memory value cache first; on a miss (or an expired memory
        entry) it falls back to the on-disk envelope (#7).
        """
        with self._lock:
            if key in self._memory_cache:
                expires, value = self._memory_cache[key]
                if expires is not None and self.clock() > expires:
                    self._memory_cache.pop(key, None)
                else:
                    self._memory_cache.move_to_end(key)
                    # Keep disk-index LRU recency in sync so eviction still
                    # protects recently used entries even when served from RAM.
                    if key in self._index:
                        self._index.move_to_end(key)
                    return value
        envelope = self._read_envelope(key)
        return envelope.get("value") if envelope is not None else None

    def cached_at_of(self, key: str, ttl: int | None = None) -> float | None:
        """Return the ``cached_at`` timestamp for a valid (unexpired) entry.

        Returns ``None`` if the key is missing, expired, or unreadable. Used by
        sitemap lastmod freshness checks to decide whether a cached page is
        newer than the sitemap's advertised last-modified time (#3).
        """
        envelope = self._read_envelope(key)
        return envelope.get("cached_at") if envelope is not None else None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store ``value`` (must be JSON-serializable) with a TTL in seconds.

        ``ttl <= 0`` means "never expires".
        """
        effective_ttl = self.default_ttl if ttl is None else ttl
        expires = None if effective_ttl <= 0 else self.clock() + effective_ttl
        # cached_at records when this entry was written so callers can compare a
        # remote "last modified" signal against our local fetch time (#3).
        envelope = {"expires": expires, "value": value, "cached_at": self.clock()}
        path = self._path_for(key)
        with self._lock:
            path.write_text(json.dumps(envelope))
            self._index[key] = path
            self._index.move_to_end(key)
            # Mirror small values into the in-memory value cache (#7).
            if sys.getsizeof(value) < self._MEMORY_CACHE_MAX_SIZE:
                self._memory_cache[key] = (expires, value)
                self._memory_cache.move_to_end(key)
                if len(self._memory_cache) > self._MEMORY_CACHE_MAX:
                    self._memory_cache.popitem(last=False)
            else:
                # A large value must not linger in the small-value cache.
                self._memory_cache.pop(key, None)
        # Enforce size limit after write, using the in-memory index (no glob).
        self._enforce_max_entries()

    def prune(self) -> int:
        """Delete expired entries; return the number removed."""
        removed = 0
        now = self.clock()
        with self._lock:
            for path in self.cache_dir.glob(f"{self.namespace}.*.json"):
                try:
                    envelope = json.loads(path.read_text())
                except (ValueError, OSError):
                    continue
                expires = envelope.get("expires")
                if expires is not None and now > expires:
                    path.unlink()
                    removed += 1
            # Reconcile the index with what remains on disk.
            self._index = OrderedDict(
                (k, v) for k, v in self._index.items() if v.exists()
            )
            # Drop expired in-memory value entries (TTL honored even in RAM).
            for _k in [
                k
                for k, (exp, _v) in self._memory_cache.items()
                if exp is not None and now > exp
            ]:
                self._memory_cache.pop(_k, None)
        return removed

    def clear(self) -> int:
        """Delete all entries in this namespace; return the number removed."""
        removed = 0
        with self._lock:
            for path in self.cache_dir.glob(f"{self.namespace}.*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
            # Drop the in-memory index so stale paths are not reused.
            self._index.clear()
            # Drop the in-memory value cache too.
            self._memory_cache.clear()
        return removed
