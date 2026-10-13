"""In-container disk content cache (no sidecar / external service).

This complements :mod:`app.transport.cache.HttpCache` (which stores only
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

from app.models.cache_envelope import CacheEnvelope
from app.models.json_value import JsonValue
from app.safety.atomic_write import atomic_write_text
from app.vocabulary.limits import (
    CONTENT_CACHE_MAX_ENTRY_BYTES,
    CONTENT_CACHE_MAX_TOTAL_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
)


def _default_clock() -> float:
    return time.time()


#: An unparsable stored expiry resolves to before the clock, so every
#: freshness comparison reads it as expired rather than raising.
_UNPARSEABLE = float("-inf")


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
        self.write_data = self._write_data if write_data is None else write_data
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
        # Small-value RAM mirror (bounded LRU, shallow size < 1MB) so hot reads
        # never touch disk; entries carry (expires, value) to honor TTL inline.
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

    def _drop_locked(self, key: str, doomed: list[tuple[str | None, Path]]) -> None:
        """Remove one entry from the index, byte accounting and RAM.

        Caller must hold ``self._lock``. The on-disk file is appended to
        ``doomed`` as a ``(key, path)`` pair for unlinking after the lock is
        released — disk I/O must never hold the lock, since the event-loop
        thread contends on it (``aget``/``aset``).
        """
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

        Re-validate under the lock first: a key re-set after its eviction
        reuses this exact path (``_path_for`` is deterministic), so unlinking
        blindly would destroy a live entry. A still-registered path is spared;
        ``key is None`` marks a file no index entry owns and it is spared when
        any live entry points at its path (a concurrent write revived it).

        Returns the number of files actually removed.
        """
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
        """Evict least-recently-used entries using the in-memory index.

        Enforces both the entry-count cap and the total byte budget.
        Runs in O(overflow) unlinks with no directory glob/stat, unlike the
        previous implementation that listed and stat() every cache file on each
        write.

        Callers invoke this OUTSIDE ``self._lock``, so this method takes the
        lock itself. That leaves a window between the caller's write section
        ending and this method acquiring the lock, in which a concurrent thread
        can read the just-written entry and find the index over budget --
        ``_over_limits`` reports True for a moment. That is benign: the entry is
        valid and consistent, and only the eviction is one step behind.

        The window is described here rather than at the write call sites because
        it is this method's acquisition that closes it, and two copies of the
        note were previously being kept in step by hand -- and had already
        drifted into describing a lock release that never happens (``_lock`` is
        not reentrant, so a caller holding it here would deadlock).
        """
        doomed: list[tuple[str | None, Path]] = []
        with self._lock:
            while self._index and self._over_limits():
                oldest_key = next(iter(self._index))
                self._drop_locked(oldest_key, doomed)
        self._unlink_doomed(doomed)

    # -- public API ---------------------------------------------------------
    def _read_envelope(self, key: str) -> CacheEnvelope | None:
        """Return the unexpired envelope for ``key`` (registering it in the LRU
        index), or ``None`` if missing, expired, or unreadable.

        Shared by :meth:`get`, :meth:`aget` and :meth:`cached_at_of` so the
        envelope-read + TTL-expiry logic lives in exactly one place.

        The file is read and parsed OUTSIDE ``self._lock``: the event-loop
        thread contends on that lock (``aget``/``aset``), and a multi-MB
        ``read_bytes`` + ``json.loads`` is exactly the work that must not hold
        it. Only the RAM state — index membership, LRU recency, the freshness
        mirror, byte accounting and the RAM value mirror — is touched under the
        lock, the same collect-under-lock / act-after-release split as
        ``_enforce_limits``/``_unlink_doomed``.
        """
        path = self._path_for(key)
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
        envelope: CacheEnvelope | None = None
        if raw is not None:
            try:
                envelope = json.loads(raw)
            except ValueError:
                envelope = None
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
                expires = self._expiry(envelope)
                if expires is not None and self.clock() > expires:
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

    @staticmethod
    def _expiry(envelope: CacheEnvelope) -> float | None:
        """The row's expiry as a comparable number, or ``None`` when it is not one.

        The envelope is JSON, so a row can carry an ``expires`` that is a string
        or a list and still parse. Every freshness test then compares the clock
        against it, and a string raises ``TypeError`` -- through the disk read,
        through the RAM mirror it warms, and through the freshness mirror. One
        bad row made every later read of that key raise.

        Such a row is corruption by the same standard as unparsable JSON, and
        corruption here is a miss: the caller refetches rather than serve a value
        whose freshness nobody can state.
        """
        raw = envelope.get("expires")
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return _UNPARSEABLE
        return float(raw)

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
        """Store into the RAM mirror under the serialized-size gate
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

    def _memory_probe(self, key: str) -> Any | None:
        """The RAM-mirror probe with ``self._lock`` taken.

        ``_memory_hit_locked`` is already the single home of the probe's rules,
        but each reader still spelled the acquire-probe-test-return dance itself,
        so the part worth having in one place was declared twice: a reader that
        probes without the lock is a data race against ``_enforce_limits``, and a
        copy of the dance is where that mistake would appear. The sync and async
        readers differ only in whether the on-disk fallback is awaited, which is
        one line each rather than a copy of the locked read.
        """
        with self._lock:
            return self._memory_hit_locked(key)

    def get(self, key: str, ttl: int | None = None) -> Any | None:
        """Return the cached value if present and unexpired, else ``None``.

        Checks the in-memory value cache first; on a miss (or an expired memory
        entry) it falls back to the on-disk envelope.
        """
        hit = self._memory_probe(key)
        if hit is not None:
            return hit
        envelope = self._read_envelope(key)
        return None if envelope is None else envelope.get("value")

    def cached_at_of(self, key: str, ttl: int | None = None) -> float | None:
        """Return the ``cached_at`` timestamp for a valid (unexpired) entry.

        Returns ``None`` if the key is missing, expired, or unreadable. Used by
        sitemap lastmod freshness checks to decide whether a cached page is
        newer than the sitemap's advertised last-modified time.

        Freshness is answered from the ``(expires, cached_at)`` metadata mirror
        so a multi-MB envelope is never read and JSON-parsed just to read a
        timestamp. A mirror miss, or an entry the mirror shows as expired, is
        routed through :meth:`_read_envelope` -- the single chokepoint for full
        reads and for every index mutation -- so the fast path can never answer
        from metadata the slow path would have expired or evicted.
        """
        with self._lock:
            entry = self._index_cached_at.get(key)
            if entry is not None:
                expires, cached_at = entry
                if expires is None or self.clock() <= expires:
                    return cached_at
        envelope = self._read_envelope(key)
        return envelope.get("cached_at") if envelope is not None else None

    def _entry_to_store(self, value: JsonValue, ttl: int | None) -> tuple[CacheEnvelope, float | None]:
        """Build the on-disk envelope for one write, and the expiry it resolved to.

        The envelope is this cache's stored contract, and the sync and async
        write paths were each building it independently -- so a field added for
        one was invisible to the other, and an entry written through the other
        path would not carry it. One owner, so the two cannot disagree about
        what an entry looks like on disk.

        Also one clock read per write. ``expires`` and ``cached_at`` came from
        separate ``clock()`` calls, so ``cached_at`` recorded a later instant
        than the one the expiry was derived from -- which is the instant a
        caller compares its remote last-modified signal against.
        """
        effective_ttl = self.default_ttl if ttl is None else ttl
        # cached_at records when this entry was written so callers can compare a
        # remote "last modified" signal against our local fetch time.
        now = self.clock()
        expires = None if effective_ttl <= 0 else now + effective_ttl
        return {"expires": expires, "value": value, "cached_at": now}, expires

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store ``value`` (must be JSON-serializable) with a TTL in seconds.

        ``ttl <= 0`` means "never expires".
        """
        envelope, expires = self._entry_to_store(value, ttl)
        # Serialize first: entries over the per-entry cap are refused before
        # any disk write, and the byte size feeds total-budget accounting.
        data = json.dumps(envelope)
        path = self._path_for(key)
        with self._lock:
            if self.max_entry_bytes is not None and len(data) > self.max_entry_bytes:
                # Refuse before any disk write: a still-valid older
                # entry for this key is kept; TTL governs its staleness.
                return
            self.write_data(path, data)
            self._commit_entry_locked(key, path, len(data))
            # Freshness metadata is known at write time; record it so later
            # cached_at_of() calls never deserialize this envelope.
            self._index_cached_at[key] = (expires, envelope["cached_at"])
            # Mirror small values into the in-memory value cache, under
            # the same gate + count cap as every other mirror write.
            self._remember_memory_locked(key, value, expires, len(data))
        self._enforce_limits()

    async def aget(self, key: str, ttl: int | None = None) -> Any | None:
        """Async ``get``: offload the (potentially large) on-disk envelope read
        and JSON parse to a worker thread so the event loop is never stalled.
        The read itself runs outside
        ``self._lock`` (see ``_read_envelope``), so a concurrent ``aset`` is
        not queued behind it."""
        hit = self._memory_probe(key)
        if hit is not None:
            return hit
        # Disk read + JSON parse off the loop, and off the lock.
        envelope = await asyncio.to_thread(self._read_envelope, key)
        return None if envelope is None else envelope.get("value")

    async def aset(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Async ``set`` with the same byte-budget contract: the value
        is serialized in a worker thread first, the size decision is made
        under the lock before any index or disk mutation, and the large disk
        write is offloaded so the event loop is never stalled."""
        # Built off the loop with the serialize: it is pure, and the envelope is
        # the sync path's own contract (``_entry_to_store``).
        envelope, expires = await asyncio.to_thread(self._entry_to_store, value, ttl)
        path = self._path_for(key)
        # Serialize FIRST: the refuse/keep decision must be made from the
        # exact bytes that will hit disk, before anything is mutated.
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
        """Record ``key``'s new path and size in the index, and the byte delta.

        The index, the per-entry sizes, the running byte total, and the LRU
        order are one accounting fact with four fields, and a reader of the
        total (``_enforce_limits``) trusts all four to agree. Written inline
        once at each commit site, it was two places to update and the async
        path had to add a comment explaining why it re-read the prior size
        under this same lock -- a property of the accounting, not of asynchrony,
        stated only where someone was already thinking about it.

        Callers must hold ``self._lock``.
        """
        old_size = self._index_sizes.get(key, 0)
        self._index[key] = path
        self._index.move_to_end(key)
        self._index_sizes[key] = size
        self._index_total_bytes += size - old_size

    def _write_data(self, path: Path, data: str) -> None:
        """The real writer, and the default behind ``write_data``: persist a
        pre-serialized envelope. ``aset`` sizes the exact bytes before writing;
        ``set`` keeps its synchronous inline write."""
        atomic_write_text(path, data)

    def prune(self) -> int:
        """Delete expired entries; return the number removed."""
        now = self.clock()
        # Glob + read + parse OUTSIDE the lock: this is the multi-MB work and
        # the event-loop thread contends on self._lock. Reconcile under it below.
        expired: list[tuple[Path, int]] = []
        surviving: dict[Path, int] = {}
        for path in self.cache_dir.glob(f"{self.namespace}.*.json"):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            try:
                envelope = json.loads(raw)
            except ValueError:
                # Corrupt but present: keep the file (the read path drops it on
                # the next access) and repair its recorded size below.
                surviving[path] = len(raw)
                continue
            expires = self._expiry(envelope)
            if expires is not None and now > expires:
                # The byte size rides the verdict: applied under the lock below,
                # only against the entry these bytes still describe.
                expired.append((path, len(raw)))
            else:
                surviving[path] = len(raw)

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
                    # A re-set landed during the scan: spare the file, keep the
                    # entry registered under its current recorded size.
                    surviving[path] = self._index_sizes[key]
            # Rebuild index and byte accounting from what survived on disk;
            # a dropped entry re-registers from disk later, like a warm restart.
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
            for _k in [k for k, (exp, _v) in self._memory_cache.items() if exp is not None and now > exp]:
                self._memory_cache.pop(_k, None)
        return self._unlink_doomed(doomed)

    def clear(self) -> int:
        """Delete all entries in this namespace; return the number removed."""
        # Snapshot the files outside the lock; the RAM state is dropped under
        # it and the unlinks run afterwards (disk I/O must never hold it).
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
