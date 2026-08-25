"""SQLite-backed HTTP conditional cache (ETag / Last-Modified).

Supports :rfc:`7232` conditional requests: a previously cached response's
``ETag`` and ``Last-Modified`` are replayed as ``If-None-Match`` /
``If-Modified-Since`` headers, and a ``304 Not Modified`` short-circuits
parsing. This lets repeated crawls of unchanged pages avoid re-downloading and
re-extracting content.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


class HttpCache:
    """Tiny per-service HTTP metadata cache backed by SQLite."""

    # Commit cadence for store(): committing on every write serializes all
    # cache writes through the SQLite connection (a bottleneck under high
    # concurrency, #12). Batch commits every N writes instead; uncommitted
    # rows are still visible to this connection's own reads (get), so
    # conditional-request correctness is unaffected within a process.
    _COMMIT_EVERY = 50

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS http_cache ("
            " url TEXT PRIMARY KEY,"
            " etag TEXT,"
            " last_modified TEXT,"
            " stored_at REAL"
            ")"
        )
        self._conn.commit()
        self._pending_writes = 0

    def get(self, url: str) -> tuple[str | None, str | None]:
        row = self._conn.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None, None
        return row[0], row[1]

    def store(self, url: str, headers: dict[str, str]) -> None:
        """Record conditional-request metadata for ``url`` from response ``headers``.

        ``etag`` and ``last-modified`` are extracted (case-insensitively) from
        the headers dict; only those two values are persisted so subsequent
        crawls can issue :rfc:`7232` conditional re-requests.
        """
        lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
        etag = lowered.get("etag")
        last_modified = lowered.get("last-modified")
        self._conn.execute(
            "INSERT INTO http_cache(url, etag, last_modified, stored_at) VALUES(?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, stored_at=excluded.stored_at",
            (url, etag, last_modified, time.time()),
        )
        # Batch commits: only flush to disk every _COMMIT_EVERY writes (#12).
        self._pending_writes += 1
        if self._pending_writes >= self._COMMIT_EVERY:
            self._conn.commit()
            self._pending_writes = 0

    def conditional_headers(self, url: str) -> dict[str, str]:
        etag, last_modified = self.get(url)
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    def close(self) -> None:
        self._conn.close()
