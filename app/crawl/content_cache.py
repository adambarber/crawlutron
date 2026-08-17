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
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _default_clock() -> float:
    return time.time()


class ContentCache:
    """Disk-backed key/value cache with per-entry TTL."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        default_ttl: int = 3600,
        namespace: str = "content",
        clock: Callable[[], float] = _default_clock,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.default_ttl = default_ttl
        self.namespace = namespace
        self.clock = clock
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- key handling -------------------------------------------------------
    @staticmethod
    def make_key(*parts: Any) -> str:
        """Build a stable cache key from arbitrary parts."""
        return "|".join(json.dumps(p, sort_keys=True, separators=(",", ":")) if not isinstance(p, str) else p for p in parts)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{self.namespace}.{digest}.json"

    # -- public API ---------------------------------------------------------
    def get(self, key: str, ttl: int | None = None) -> Any | None:
        """Return the cached value if present and unexpired, else ``None``."""
        path = self._path_for(key)
        with self._lock:
            if not path.exists():
                return None
            try:
                envelope = json.loads(path.read_text())
            except (ValueError, OSError):
                return None
        expires = envelope.get("expires")
        if expires is not None and self.clock() > expires:
            return None
        return envelope.get("value")

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store ``value`` (must be JSON-serializable) with a TTL in seconds.

        ``ttl <= 0`` means "never expires".
        """
        effective_ttl = self.default_ttl if ttl is None else ttl
        expires = None if effective_ttl <= 0 else self.clock() + effective_ttl
        envelope = {"expires": expires, "value": value}
        path = self._path_for(key)
        with self._lock:
            path.write_text(json.dumps(envelope))

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
        return removed
