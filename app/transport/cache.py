"""In-memory HTTP conditional cache (ETag / Last-Modified).

Supports :rfc:`7232` conditional requests: a previously cached response's
``ETag`` and ``Last-Modified`` are replayed as ``If-None-Match`` /
``If-Modified-Since`` headers, and a ``304 Not Modified`` short-circuits
parsing. This lets repeated crawls of unchanged pages avoid re-downloading and
re-extracting content.

Design notes:

* Conditional-request metadata is tiny (two short strings per normalized URL)
  and process-local, so an in-process ``dict`` with TTL + LRU eviction is both
  strictly faster and free of blocking SQL/disk on the event loop. The previous
  SQLite-backed store serialized all cache writes through one ``check_same_thread
  =False`` connection with no lock -- a crash-under-load hazard on the async
  loop. A plain dict guarded by a lock is correct under the GIL for this
  access pattern.
* The cache is intentionally in-memory. Persistence across restarts is out of
  scope here (conditional revalidation is a within-session fast path); content
  caching belongs to :class:`app.transport.content_cache.ContentCache`.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable

from app.models.normalized_url import NormalizedUrl

_DEFAULT_TTL_SECONDS = 3600.0
_MAX_ENTRIES = 100_000


class HttpCache:
    """Tiny in-process HTTP metadata cache (ETag / Last-Modified) with TTL."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        # url -> (etag, last_modified, expires_at); OrderedDict insertion order
        # gives FIFO eviction for the LRU bound.
        self._data: dict[NormalizedUrl, tuple[str | None, str | None, float]] = {}
        self._closed = False

    def _purge_expired(self, now: float) -> None:
        """Drop expired entries (called while holding ``_lock``)."""
        if not self._ttl_seconds:
            return
        expired = [url for url, (_, _, exp) in self._data.items() if exp <= now]
        for url in expired:
            self._data.pop(url, None)

    def _evict_if_needed(self) -> None:
        if len(self._data) > self._max_entries:
            # Fork/insert order is the same as this dict's iteration order, so
            # drop the earliest-inserted entries (FIFO) to bound memory.
            # The snapshot is bounded to the overflow itself:
            # drawing `over` keys through islice avoids materializing the full
            # key list (up to 100k) per store while still snapshotting before
            # popping, so mutation never happens mid-iteration.
            over = len(self._data) - self._max_entries
            for url in list(itertools.islice(self._data, over)):
                self._data.pop(url, None)

    def get(self, url: NormalizedUrl) -> tuple[str | None, str | None]:
        now = self._clock()
        with self._lock:
            entry = self._data.get(url)
            if entry is None:
                return None, None
            etag, last_modified, expires = entry
            if self._ttl_seconds and expires <= now:
                self._data.pop(url, None)
                return None, None
            return etag, last_modified

    def store(self, url: NormalizedUrl, headers: dict[str, str]) -> None:
        """Record conditional-request metadata for ``url`` from response ``headers``.

        ``etag`` and ``last-modified`` are extracted (case-insensitively) from
        the headers dict; only those two values are persisted so subsequent
        crawls can issue :rfc:`7232` conditional re-requests.
        """
        lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
        etag = lowered.get("etag")
        last_modified = lowered.get("last-modified")
        now = self._clock()
        expires = now + self._ttl_seconds if self._ttl_seconds else 0.0
        with self._lock:
            # Re-insert so a refreshed entry moves to the back of the FIFO order.
            self._data.pop(url, None)
            self._data[url] = (etag, last_modified, expires)
            self._evict_if_needed()

    def conditional_headers(self, url: NormalizedUrl) -> dict[str, str]:
        etag, last_modified = self.get(url)
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    def close(self) -> None:
        """No-op: the cache is in-memory and shares no external resources."""
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed
