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

import asyncio
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.atomic_write import atomic_write_text
from app.constants import (
    CONTENT_CACHE_MAX_ENTRY_BYTES,
    CONTENT_CACHE_MAX_TOTAL_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
)


def _default_clock() -> float:
    return time.time()


class ContentCache:
    """Disk-backed key/value cache with per-entry TTL and optional LRU eviction."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        default_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        namespace: str = "content",
        clock: Callable[[], float] = _default_clock,
        max_entries: int = 10000,
        max_entry_bytes: int = CONTENT_CACHE_MAX_ENTRY_BYTES,
        max_total_bytes: int = CONTENT_CACHE_MAX_TOTAL_BYTES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.default_ttl = default_ttl
        self.namespace = namespace
        self.clock = clock
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_bytes
        self.max_total_bytes = max_total_bytes
        # Parallel byte accounting for the LRU index (H9): entry count alone
        # cannot bound disk use when one envelope holds a multi-MB body.
        self._index_sizes: dict[str, int] = {}
        self._index_total_bytes: int = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # In-memory LRU index (key -> path) so eviction never globs/stats the
        # whole cache directory on the hot path (#5). The on-disk file remains
        # the source of truth for values; this index only tracks recency/order.
        # (A warm restart with pre-existing files repopulates the index lazily
        # on first get/set of each key; prune() reconciles disk vs index.)
        self._index: OrderedDict[str, Path] = OrderedDict()
        # In-memory value cache: keep small values resident so repeated reads
        # of a hot URL never touch disk. Bounded LRU (max 100 entries) of
        # entries whose shallow size is < 1MB. The on-disk file remains the
        # source of truth; this is purely a read accelerator. Entries store a
        # (expires, value) tuple so get() can honor TTL without a disk read.
        self._memory_cache: OrderedDict[str, tuple[float | None, Any]] = OrderedDict()
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

    def _drop_locked(self, key: str) -> None:
        """Remove one entry from the index, byte accounting, RAM and disk."""
        old_path = self._index.pop(key, None)
        freed = self._index_sizes.pop(key, 0)
        self._index_total_bytes = max(0, self._index_total_bytes - freed)
        if old_path is not None:
            try:
                old_path.unlink()
            except OSError:
                pass
        # Keep the memory value cache consistent with evicted disk keys.
        self._memory_cache.pop(key, None)

    def _over_limits(self) -> bool:
        over_count = (
            self.max_entries is not None and self.max_entries > 0 and len(self._index) > self.max_entries
        )
        over_bytes = (
            self.max_total_bytes is not None
            and self.max_total_bytes > 0
            and self._index_total_bytes > self.max_total_bytes
        )
        return over_count or over_bytes

    def _enforce_limits(self) -> None:
        """Evict least-recently-used entries using the in-memory index.

        Enforces both the entry-count cap and the total byte budget (H9).
        Runs in O(overflow) unlinks with no directory glob/stat, unlike the
        previous implementation that listed and stat() every cache file on each
        write (#5).
        """
        with self._lock:
            while self._index and self._over_limits():
                oldest_key = next(iter(self._index))
                self._drop_locked(oldest_key)

    def _enforce_max_entries(self) -> None:
        """Backward-compatible alias for the count+byte enforcement."""
        self._enforce_limits()

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

    def _memory_hit_locked(self, key: str) -> Any | None:
        """RAM-mirror probe (callers must hold ``self._lock``).

        Returns the cached value on a fresh hit (bumping RAM + disk-index LRU
        recency), ``None`` on miss or expiry. The single home of the probe so
        the sync and async paths cannot drift. ``None`` is never a stored
        value: envelopes without a value are misses, not members.
        """
        if key not in self._memory_cache:
            return None
        expires, value = self._memory_cache[key]
        if expires is not None and self.clock() > expires:
            self._memory_cache.pop(key, None)
            return None
        self._memory_cache.move_to_end(key)
        # Keep disk-index LRU recency in sync so eviction still protects
        # recently used entries even when served from RAM.
        if key in self._index:
            self._index.move_to_end(key)
        return value

    def _remember_memory_locked(self, key: str, value: Any, expires: float | None, size: int) -> None:
        """Store into the RAM mirror under the serialized-size gate (N5/final #1)
        and the entry-count cap — the single home of the store so no path can
        lose either bound. Oversized values are dropped, never mirrored.
        """
        if value is None or size >= self._MEMORY_CACHE_MAX_SIZE:
            # A large value must not linger in the small-value cache.
            self._memory_cache.pop(key, None)
            return
        self._memory_cache[key] = (expires, value)
        self._memory_cache.move_to_end(key)
        if len(self._memory_cache) > self._MEMORY_CACHE_MAX:
            self._memory_cache.popitem(last=False)

    def _entry_size_on_disk_locked(self, key: str) -> int:
        """Serialized size of ``key``'s envelope for the mirror gate.

        Gate on the SERIALIZED size (H2): shallow sys.getsizeof sees only the
        container. Falls back to the cap when the size cannot be established,
        so an unknown-size entry can never sneak into the RAM mirror.
        """
        entry_size = self._index_sizes.get(key)
        if entry_size is None:
            try:
                entry_size = self._path_for(key).stat().st_size
            except OSError:
                entry_size = self._MEMORY_CACHE_MAX_SIZE
        return entry_size

    def get(self, key: str, ttl: int | None = None) -> Any | None:
        """Return the cached value if present and unexpired, else ``None``.

        Checks the in-memory value cache first; on a miss (or an expired memory
        entry) it falls back to the on-disk envelope (#7).
        """
        with self._lock:
            hit = self._memory_hit_locked(key)
            if hit is not None:
                return hit
        envelope = self._read_envelope(key)
        if envelope is None:
            return None
        value = envelope.get("value")
        # Warm the mirror on the sync disk hit too (same accelerator contract
        # as aget): a hot URL must stop touching disk after the first read.
        # The mirror/index update runs under the lock, exactly like aget, so
        # the multi-step store stays atomic against concurrent writers.
        expires = envelope.get("expires")
        with self._lock:
            self._remember_memory_locked(key, value, expires, self._entry_size_on_disk_locked(key))
        return value

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
        # Serialize first: entries over the per-entry cap are refused before
        # any disk write (H9), and the byte size feeds total-budget accounting.
        data = json.dumps(envelope)
        path = self._path_for(key)
        with self._lock:
            if self.max_entry_bytes is not None and len(data) > self.max_entry_bytes:
                # Refuse before any disk write (N5): a still-valid older
                # entry for this key is kept; TTL governs its staleness.
                return
            old_size = self._index_sizes.get(key, 0)
            self._write_data(path, data)
            self._index[key] = path
            self._index.move_to_end(key)
            self._index_sizes[key] = len(data)
            self._index_total_bytes += len(data) - old_size
            # Mirror small values into the in-memory value cache (#7), under
            # the same gate + count cap as every other mirror write.
            self._remember_memory_locked(key, value, expires, len(data))
        # Enforce count + byte limits after write, using the in-memory index
        # (no glob). NOTE: this call briefly releases ``self._lock`` between
        # the write and the eviction (re-acquired inside ``_enforce_limits``).
        # During that window another thread can read the just-written entry
        # and observe an over-sized index. This is benign — the entry is
        # valid and consistent; only the eviction is one step behind.
        self._enforce_limits()

    async def aget(self, key: str, ttl: int | None = None) -> Any | None:
        """Async ``get``: offload the (potentially large) on-disk envelope read
        and JSON parse to a worker thread so the event loop is never stalled
        (adversarial perf #1.2 / #2.2)."""
        with self._lock:
            hit = self._memory_hit_locked(key)
            if hit is not None:
                return hit
        # Disk read + JSON parse off the loop.
        envelope = await asyncio.to_thread(self._read_envelope, key)
        if envelope is None:
            return None
        value = envelope.get("value")
        expires = envelope.get("expires")
        with self._lock:
            self._remember_memory_locked(key, value, expires, self._entry_size_on_disk_locked(key))
        return value

    async def aset(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Async ``set`` with the same byte-budget contract (N1/H9): the value
        is serialized in a worker thread first, the size decision is made
        under the lock before any index or disk mutation, and the large disk
        write is offloaded so the event loop is never stalled."""
        effective_ttl = self.default_ttl if ttl is None else ttl
        expires = None if effective_ttl <= 0 else self.clock() + effective_ttl
        envelope = {"expires": expires, "value": value, "cached_at": self.clock()}
        path = self._path_for(key)
        # Serialize FIRST: the refuse/keep decision must be made from the
        # exact bytes that will hit disk, before anything is mutated.
        data = await asyncio.to_thread(json.dumps, envelope)
        size = len(data)
        with self._lock:
            if self.max_entry_bytes is not None and size > self.max_entry_bytes:
                # Refuse before any disk write (N1): the payload never hits
                # disk and any still-valid older entry is kept (N5).
                return
        try:
            await asyncio.to_thread(self._write_data, path, data)
        except BaseException:
            # Nothing above mutated the index, byte totals, or RAM mirror before
            # this point, and atomic_write_text leaves the prior file byte-
            # identical on failure — so the pre-write state is already exactly
            # consistent. A prior valid entry stays reachable and a new key
            # stays absent; there is nothing to roll back. Just propagate (N1).
            raise
        with self._lock:
            # Read the prior size under the commit lock (like sync set()), so a
            # concurrent commit between the size-check and here can't leave the
            # byte total off by the stale delta.
            old_size = self._index_sizes.get(key, 0)
            self._index[key] = path
            self._index.move_to_end(key)
            self._index_sizes[key] = size
            self._index_total_bytes += size - old_size
            # Mirror only after the write commits, under the shared gate.
            self._remember_memory_locked(key, value, expires, size)
        # Enforce count + byte limits after write, using the in-memory index
        # (no glob). NOTE: this call briefly releases ``self._lock`` between
        # the write and the eviction (re-acquired inside ``_enforce_limits``).
        # During that window another thread can read the just-written entry
        # and observe an over-sized index. This is benign — the entry is
        # valid and consistent; only the eviction is one step behind.
        self._enforce_limits()

    def _write_data(self, path: Path, data: str) -> None:
        """Persist a pre-serialized envelope (N1: ``aset`` sizes the exact
        bytes before writing; ``set`` keeps its synchronous inline write)."""
        atomic_write_text(path, data)

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
            # Reconcile the index with what remains on disk, drop byte
            # accounting for entries that are gone, and repair sizes for
            # entries whose recorded size is unknown (N1: e.g. a crash
            # between index insert and accounting update).
            self._index = OrderedDict((k, v) for k, v in self._index.items() if v.exists())
            repaired_total = 0
            for k, v in self._index.items():
                size = self._index_sizes.get(k)
                if size is None:
                    try:
                        size = v.stat().st_size
                    except OSError:
                        size = 0
                    self._index_sizes[k] = size
                repaired_total += size
            self._index_sizes = {k: s for k, s in self._index_sizes.items() if k in self._index}
            self._index_total_bytes = repaired_total
            # Drop expired in-memory value entries (TTL honored even in RAM).
            for _k in [k for k, (exp, _v) in self._memory_cache.items() if exp is not None and now > exp]:
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
            # Drop byte accounting and the in-memory value cache too.
            self._index_sizes.clear()
            self._index_total_bytes = 0
            self._memory_cache.clear()
        return removed
