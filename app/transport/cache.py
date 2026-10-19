"""In-memory HTTP conditional cache (ETag / Last-Modified): :rfc:`7232` headers
are replayed and a ``304`` short-circuits parsing, so unchanged pages avoid
re-downloading. In-process metadata under a lock, not persisted.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable

from app.models.normalized_url import NormalizedUrl
from app.transport.cookie_jar import COOKIE_NAMESPACE

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
        # (namespace, url) -> (etag, last_modified, expires_at), namespaced so one caller's
        # validator is never replayed on another's crawl; mirrors the content-cache partitioning.
        self._data: dict[tuple[str, NormalizedUrl], tuple[str | None, str | None, float]] = {}
        self._closed = False

    def _evict_if_needed(self) -> None:
        if len(self._data) > self._max_entries:
            # drop the earliest-inserted entries (FIFO) to bound memory: the snapshot is
            # bounded to the overflow, so a key list of up to 100k is never materialized.
            over = len(self._data) - self._max_entries
            for url in list(itertools.islice(self._data, over)):
                self._data.pop(url, None)

    def _key(self, url: NormalizedUrl) -> tuple[str, NormalizedUrl]:
        """The memory key for ``url`` under the active cookie namespace."""
        return (COOKIE_NAMESPACE.get(), url)

    def get(self, url: NormalizedUrl) -> tuple[str | None, str | None]:
        now = self._clock()
        key = self._key(url)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None, None
            etag, last_modified, expires = entry
            if self._ttl_seconds and expires <= now:
                self._data.pop(key, None)
                return None, None
            return etag, last_modified

    def store(self, url: NormalizedUrl, headers: dict[str, str]) -> None:
        """Record conditional-request metadata for ``url`` from response ``headers``:
        only ``etag`` and ``last-modified`` (case-insensitive) are persisted, so
        later crawls can issue :rfc:`7232` conditional re-requests.
        """
        lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
        etag = lowered.get("etag")
        last_modified = lowered.get("last-modified")
        now = self._clock()
        expires = now + self._ttl_seconds if self._ttl_seconds else 0.0
        key = self._key(url)
        with self._lock:
            # Re-insert so a refreshed entry moves to the back of the FIFO order.
            self._data.pop(key, None)
            self._data[key] = (etag, last_modified, expires)
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
