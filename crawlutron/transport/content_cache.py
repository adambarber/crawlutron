"""Disk content cache: JSON-enveloped, TTL'd, thread-safe, no sidecar.

Persists fetched responses so unchanged URLs skip re-download; pairs with the
ETag-only ``HttpCache``. TTL uses an injectable clock for deterministic tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from crawlutron.models.cache_envelope import CacheEnvelope
from crawlutron.models.json_value import JsonValue
from crawlutron.transport.content_cache_internals import (
    TMP_ORPHAN_MAX_AGE_SECONDS,
    atomic_write,
    decode_envelope,
    resolve_expiry,
    scan_cache_dir,
)
from crawlutron.transport.freshness import default_clock, expiry_for, is_expired, stores_for_ttl
from crawlutron.vocabulary.limits import (
    CONTENT_CACHE_MAX_ENTRY_BYTES,
    CONTENT_CACHE_MAX_TOTAL_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
)

# RAM mirror bounds for small-valued hot reads: a value at or past the size gate
# is never mirrored, and at most this many small entries stay resident in memory.
MEMORY_CACHE_MAX_SIZE = 1024 * 1024
MEMORY_CACHE_MAX = 100


def _tmp_is_owned(path: Path) -> bool:
    """Whether a live ``atomic_write_text`` holds the temp's advisory lock.
    Taking the lock IS the ownership test: a held or unopenable path reports
    owned, so a live write is never unlinked by age alone."""
    import fcntl

    try:
        probe = os.open(path, os.O_RDONLY)
    except OSError:
        return True
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        return False
    finally:
        os.close(probe)


def _sweep_orphan_tmps(cache_dir: Path, namespace: str, now: float) -> int:
    """Remove ``.tmp`` leftovers from a crashed atomic_write; age spares a live
    write, since neither the value glob nor budget ever sees the file."""
    cutoff = now - TMP_ORPHAN_MAX_AGE_SECONDS
    removed = 0
    for path in cache_dir.glob(f".{namespace}.*.tmp"):
        try:
            if path.stat().st_mtime > cutoff or _tmp_is_owned(path):
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


class ContentCache:
    """Disk-backed key/value cache with per-entry TTL and optional LRU eviction."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        default_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        namespace: str = "content",
        clock: Callable[[], float] = default_clock,
        max_entries: int = 10000,
        max_entry_bytes: int = CONTENT_CACHE_MAX_ENTRY_BYTES,
        max_total_bytes: int | None = CONTENT_CACHE_MAX_TOTAL_BYTES,
        write_data: Callable[[Path, str], None] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.default_ttl = default_ttl
        self.namespace = namespace
        self.clock = clock
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_bytes
        # ``None`` means "no total-bytes cap" and ``_over_limits`` branches on
        # exactly that; turning it off is a deliberate, visible argument.
        self.max_total_bytes = max_total_bytes
        # The single disk-write seam: a caller that must watch, fail or relocate
        # writes hands over a callable -- no test rebinds a private method.
        self.write_data = atomic_write if write_data is None else write_data
        # Parallel byte accounting for the LRU index: entry count alone
        # cannot bound disk use when one envelope holds a multi-MB body.
        self._index_sizes: dict[str, int] = {}
        self._index_total_bytes: int = 0
        # (expires, cached_at) mirror per live key: freshness checks must not
        # deserialize multi-MB envelopes to read a timestamp. _read_envelope mutates.
        self._index_cached_at: dict[str, tuple[float | None, float | None]] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # In-memory LRU index (key -> path) so eviction never globs the cache
        # directory on the hot path; disk stays the source of truth for values.
        self._index: OrderedDict[str, Path] = OrderedDict()
        # Small-value RAM mirror (bounded LRU, shallow < 1MB) for hot reads.
        self._memory_cache: OrderedDict[str, tuple[float | None, JsonValue]] = OrderedDict()
        self._MEMORY_CACHE_MAX = MEMORY_CACHE_MAX
        self._MEMORY_CACHE_MAX_SIZE = MEMORY_CACHE_MAX_SIZE

    # -- key handling -------------------------------------------------------
    @staticmethod
    def make_key(*parts: JsonValue) -> str:
        """Build a stable cache key from arbitrary parts."""
        return "|".join(json.dumps(p, sort_keys=True, separators=(",", ":")) for p in parts)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{self.namespace}.{digest}.json"

    def _drop_locked(self, key: str, doomed: list[tuple[str | None, Path]]) -> None:
        """Remove one entry from the index, byte accounting and RAM.
        Caller holds ``self._lock``; the doomed file unlinks after release,
        because disk I/O never holds the lock."""
        old_path = self._index.pop(key, None)
        freed = self._index_sizes.pop(key, 0)
        self._index_total_bytes = max(0, self._index_total_bytes - freed)
        # Stale freshness metadata must not outlive its entry.
        self._index_cached_at.pop(key, None)
        if old_path is not None:
            doomed.append((key, old_path))
        # Keep the memory value cache consistent with evicted disk keys.
        self._memory_cache.pop(key, None)

    def _unlink_doomed(self, doomed: list[tuple[str | None, Path]]) -> int:
        """Unlink evicted/expired files after the lock is released.
        Re-validate under the lock: a re-set key reuses this exact path, and
        ``key is None`` files are spared when a live entry owns the path."""
        removed = 0
        for key, path in doomed:
            with self._lock:
                if key is not None:
                    if self._index.get(key) == path:
                        continue
                elif path in self._index.values():
                    continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def _over_limits(self) -> bool:
        over_count = self.max_entries > 0 and len(self._index) > self.max_entries
        over_bytes = (
            self.max_total_bytes is not None
            and self.max_total_bytes > 0
            and self._index_total_bytes > self.max_total_bytes
        )
        return over_count or over_bytes

    def _enforce_limits(self) -> None:
        """Evict LRU entries past the count or byte caps.
        O(overflow) unlinks, no glob/stat; invoked OUTSIDE the lock. The
        window where a reader sees an index over budget is benign."""
        doomed: list[tuple[str | None, Path]] = []
        with self._lock:
            while self._index and self._over_limits():
                oldest_key = next(iter(self._index))
                self._drop_locked(oldest_key, doomed)
        self._unlink_doomed(doomed)

    # -- public API ---------------------------------------------------------
    def _read_envelope(self, key: str) -> CacheEnvelope | None:
        """The unexpired envelope for ``key`` (LRU-registered), else ``None``. Reads outside the lock."""
        path = self._path_for(key)
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
        envelope: CacheEnvelope | None = None
        if raw is not None:
            envelope = decode_envelope(raw)
        doomed: list[tuple[str | None, Path]] = []
        try:
            with self._lock:
                if raw is None:
                    # No readable file. A stale index entry would keep counting
                    # phantom bytes, so free the accounting here too.
                    if key in self._index:
                        self._drop_locked(key, doomed)
                    return None
                if envelope is None:
                    # Corrupt: the entry can never be served, so neither the
                    # accounting nor the freshness mirror may keep it.
                    self._drop_locked(key, doomed)
                    return None
                expires = resolve_expiry(envelope)
                if is_expired(self.clock(), expires):
                    # Free the byte accounting too: phantom bytes would keep
                    # ``_over_limits`` true and evict LIVE entries later.
                    self._drop_locked(key, doomed)
                    return None
                if key not in self._index:
                    # Register a cold on-disk entry with the exact byte size
                    # just read -- every later size lookup stays off the disk.
                    self._index[key] = path
                    self._index_total_bytes += len(raw) - self._index_sizes.pop(key, 0)
                    self._index_sizes[key] = len(raw)
                # Mark recently used (LRU) by moving to the end of the ordered index.
                self._index.move_to_end(key)
                # Both mirrors re-check currency under the lock: a concurrent
                # write landing between read and commit must not be shadowed.
                if self._index_sizes.get(key) == len(raw):
                    # Remember the freshness metadata so later cached_at_of()
                    # calls are answered without re-reading the envelope.
                    self._index_cached_at[key] = (expires, envelope.get("cached_at"))
                    # Warm the RAM mirror under the same lock, gated on the
                    # exact bytes just read -- a cold read cannot smuggle multi-MB.
                    self._remember_memory_locked(key, envelope.get("value"), expires, len(raw))
                return envelope
        finally:
            self._unlink_doomed(doomed)

    def _memory_hit_locked(self, key: str) -> JsonValue | None:
        """RAM-mirror probe: value on a fresh hit (bumping recency), else ``None``."""
        if key not in self._memory_cache:
            return None
        expires, value = self._memory_cache[key]
        if is_expired(self.clock(), expires):
            self._memory_cache.pop(key, None)
            return None
        self._memory_cache.move_to_end(key)
        # Keep disk-index LRU recency in sync so eviction still protects
        # recently used entries even when served from RAM.
        if key in self._index:
            self._index.move_to_end(key)
        return value

    def _remember_memory_locked(self, key: str, value: JsonValue, expires: float | None, size: int) -> None:
        """Store into the RAM mirror under the size gate and count cap."""
        if value is None or size >= self._MEMORY_CACHE_MAX_SIZE:
            # A large value must not linger in the small-value cache.
            self._memory_cache.pop(key, None)
            return
        self._memory_cache[key] = (expires, value)
        self._memory_cache.move_to_end(key)
        if len(self._memory_cache) > self._MEMORY_CACHE_MAX:
            self._memory_cache.popitem(last=False)

    def _memory_probe(self, key: str) -> JsonValue | None:
        """The RAM-mirror probe with ``self._lock`` taken."""
        with self._lock:
            return self._memory_hit_locked(key)

    def get(self, key: str) -> JsonValue | None:
        """The cached value if present and unexpired, else ``None``.

        Memory first, then the on-disk envelope."""
        hit = self._memory_probe(key)
        if hit is not None:
            return hit
        envelope = self._read_envelope(key)
        return None if envelope is None else envelope.get("value")

    def cached_at_of(self, key: str) -> float | None:
        """``cached_at`` for a valid (unexpired) entry, else ``None``.
        Served from the ``(expires, cached_at)`` mirror so a multi-MB
        envelope is never read for a timestamp."""
        with self._lock:
            entry = self._index_cached_at.get(key)
            if entry is not None:
                expires, cached_at = entry
                if not is_expired(self.clock(), expires):
                    return cached_at
        envelope = self._read_envelope(key)
        return envelope.get("cached_at") if envelope is not None else None

    def _entry_to_store(self, value: JsonValue, ttl: int | None) -> tuple[CacheEnvelope, float | None]:
        """The on-disk envelope for one write, and its expiry.
        One owner keeps the stored contract undivided across sync/async, and
        one clock read keeps ``cached_at`` aligned with ``expires``."""
        effective_ttl = self.default_ttl if ttl is None else ttl
        # cached_at records when this entry was written so callers can compare a
        # remote "last modified" signal against our local fetch time.
        now = self.clock()
        expires = expiry_for(now, float(effective_ttl))
        return {"expires": expires, "value": value, "cached_at": now}, expires

    def set(self, key: str, value: JsonValue, ttl: int | None = None) -> None:
        """Store ``value`` (JSON-serializable) with a TTL in seconds; ``ttl <= 0`` stores nothing."""
        if not stores_for_ttl(self.default_ttl if ttl is None else ttl):
            return  # OFF convention: a non-positive TTL refuses the store.
        envelope, expires = self._entry_to_store(value, ttl)
        # Serialize first: the size feeds the per-entry cap and byte accounting.
        data = json.dumps(envelope)
        size = len(data)
        path = self._path_for(key)
        with self._lock:
            # Refuse before any disk write: a still-valid older entry is kept.
            if self.max_entry_bytes is not None and size > self.max_entry_bytes:
                return
        self.write_data(path, data)
        with self._lock:
            # The prior size is read here (not before the check) so a concurrent
            # commit cannot leave the byte total off by a stale delta.
            self._commit_entry_locked(key, path, size)
            self._index_cached_at[key] = (expires, envelope["cached_at"])
            self._remember_memory_locked(key, value, expires, size)
        self._enforce_limits()

    async def aget(self, key: str) -> JsonValue | None:
        """Async ``get``: the envelope read + parse go off the loop and off the lock."""
        hit = self._memory_probe(key)
        if hit is not None:
            return hit
        # Disk read + JSON parse off the loop, and off the lock.
        envelope = await asyncio.to_thread(self._read_envelope, key)
        return None if envelope is None else envelope.get("value")

    async def aset(self, key: str, value: JsonValue, ttl: int | None = None) -> None:
        """Async ``set`` with the same byte-budget and OFF-convention contract."""
        if not stores_for_ttl(self.default_ttl if ttl is None else ttl):
            return  # ttl <= 0 stores nothing, like the sync path.
        # Serialize off the loop: pure, and the sync path's own contract.
        envelope, expires = await asyncio.to_thread(self._entry_to_store, value, ttl)
        path = self._path_for(key)
        # Serialize FIRST: the refuse/keep decision is made from the exact bytes.
        data = await asyncio.to_thread(json.dumps, envelope)
        size = len(data)
        with self._lock:
            if self.max_entry_bytes is not None and size > self.max_entry_bytes:
                # Refuse before any disk write: the payload never hits
                # disk and any still-valid older entry is kept.
                return
        try:
            await asyncio.to_thread(self.write_data, path, data)
        except BaseException:
            # Nothing above mutated on failure and atomic_write_text leaves the
            # prior file intact: the pre-write state is consistent. Just propagate.
            raise
        with self._lock:
            # The prior size is read here (not before the check) so a concurrent
            # commit cannot leave the byte total off by a stale delta.
            self._commit_entry_locked(key, path, size)
            # Mirror only after the write commits, under the shared gate.
            self._remember_memory_locked(key, value, expires, size)
        self._enforce_limits()

    def _commit_entry_locked(self, key: str, path: Path, size: int) -> None:
        """Record path, per-entry size and byte delta in the index; caller holds the lock."""
        old_size = self._index_sizes.get(key, 0)
        self._index[key] = path
        self._index.move_to_end(key)
        self._index_sizes[key] = size
        self._index_total_bytes += size - old_size

    def prune(self) -> int:
        """Delete expired entries; return the number removed."""
        now = self.clock()
        expired, surviving = scan_cache_dir(self.cache_dir, self.namespace, resolve_expiry, now)

        doomed: list[tuple[str | None, Path]] = []
        with self._lock:
            # Verdicts are re-validated before application: a key re-set since
            # the glob has a new TTL and size; the old verdict must not expire it.
            by_path: dict[Path, str] = {p: k for k, p in self._index.items()}
            for path, size in expired:
                key = by_path.get(path)
                if key is None:
                    doomed.append((None, path))  # no index entry owns it
                elif self._index_sizes.get(key) == size:
                    self._drop_locked(key, doomed)
                else:
                    surviving[path] = self._index_sizes[key]  # re-set during the scan: spare it
            # Rebuild index and byte accounting from what survived on disk.
            live: OrderedDict[str, Path] = OrderedDict(
                (k, p) for k, p in self._index.items() if p in surviving
            )
            for stale_key in [k for k in self._index if k not in live]:
                self._index_cached_at.pop(stale_key, None)
                self._memory_cache.pop(stale_key, None)
            self._index = live
            self._index_sizes = {k: surviving[p] for k, p in live.items()}
            self._index_total_bytes = sum(self._index_sizes.values())
            # Drop expired in-memory value entries (TTL honored even in RAM).
            for _k in [k for k, (exp, _v) in self._memory_cache.items() if is_expired(now, exp)]:
                self._memory_cache.pop(_k, None)
        removed = self._unlink_doomed(doomed)
        return removed + _sweep_orphan_tmps(self.cache_dir, self.namespace, now)

    def clear(self) -> int:
        """Delete all entries in this namespace; return the number removed."""
        # Snapshot the files outside the lock; the unlinks run afterwards.
        paths = list(self.cache_dir.glob(f"{self.namespace}.*.json"))
        doomed: list[tuple[str | None, Path]] = []
        with self._lock:
            indexed: set[Path] = set()
            for key, path in list(self._index.items()):
                doomed.append((key, path))
                indexed.add(path)
            # Drop the in-memory index so stale paths are not reused.
            self._index.clear()
            # Drop byte accounting and the in-memory value cache too.
            self._index_sizes.clear()
            self._index_total_bytes = 0
            self._index_cached_at.clear()
            self._memory_cache.clear()
        doomed.extend((None, path) for path in paths if path not in indexed)
        return self._unlink_doomed(doomed)
