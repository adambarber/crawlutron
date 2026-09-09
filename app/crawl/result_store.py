"""Persist completed single-request crawl results for later retrieval.

Fixes two problems: the 202 webhook response advertises ``status_url`` that
previously did not exist, and a dropped HTTP connection mid-response meant the
crawl work was lost. Completed ``UnifiedCrawlResponse`` payloads are stored
keyed by ``request_id`` with TTL + entry-count retention guardrails.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class CrawlResultStore:
    """SQLite-backed store of completed crawl responses keyed by request id."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 1000,
        clock=None,
    ) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                request_id TEXT PRIMARY KEY,
                stored_at REAL NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def put(self, request_id: str, payload: dict) -> None:
        """Store a completed response; prune expired/oldest entries first."""
        stored_at = self._clock()
        with self._lock:
            self._prune(stored_at)
            self._conn.execute(
                "INSERT OR REPLACE INTO results (request_id, stored_at, payload) VALUES (?, ?, ?)",
                (request_id, stored_at, json.dumps(payload)),
            )
            self._conn.commit()
            # Enforce entry cap after insert (drop oldest beyond the cap).
            self._conn.execute(
                """
                DELETE FROM results WHERE request_id IN (
                    SELECT request_id FROM results ORDER BY stored_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self._max_entries,),
            )
            self._conn.commit()

    def get(self, request_id: str) -> dict | None:
        """Return the stored payload, or None if absent/expired."""
        now = self._clock()
        cutoff = now - self._ttl_seconds if self._ttl_seconds > 0 else 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT stored_at, payload FROM results WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        stored_at, payload = row
        if self._ttl_seconds > 0 and stored_at < cutoff:
            return None
        return json.loads(payload)

    def _prune(self, now: float) -> None:
        if self._ttl_seconds <= 0:
            return
        self._conn.execute("DELETE FROM results WHERE stored_at < ?", (now - self._ttl_seconds,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
