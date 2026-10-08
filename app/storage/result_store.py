"""Persist completed single-request crawl results for later retrieval.

Fixes two problems: the 202 webhook response advertises ``status_url`` that
previously did not exist, and a dropped HTTP connection mid-response meant the
crawl work was lost. Completed ``UnifiedCrawlResponse`` payloads are stored
keyed by ``request_id`` with TTL + entry-count retention guardrails.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from app.models.json_value import JsonValue
from app.storage.coordination_store import CoordinationStore
from app.storage.migrations import StoreSchema


class CrawlResultStore(CoordinationStore):
    """SQLite-backed store of completed crawl responses keyed by request id.

    The connection, its PRAGMAs, the intra-process lock and ``close`` are the
    base's. This store used to open its own connection and keep its own lock,
    which made it the one store that could drift from the shared WAL/busy-timeout
    configuration and the one whose ``close`` was written twice.
    """

    schema = StoreSchema.RESULTS

    def __init__(
        self,
        db_path: str | Path,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 1000,
        clock=None,
    ) -> None:
        super().__init__(db_path)
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock or time.time

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

    def payloads(self) -> list[JsonValue]:
        """Every retained payload, newest first.

        The same TTL filter as :meth:`get`, applied here too: an expired row
        that still answered a rollup would have the store reporting evidence it
        has already declared gone. Ordering is newest-first so a caller reading
        a bounded prefix sees the most recent records.
        """
        now = self._clock()
        cutoff = now - self._ttl_seconds if self._ttl_seconds > 0 else 0.0
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM results WHERE stored_at >= ? ORDER BY stored_at DESC",
                (cutoff,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def aput(self, request_id: str, payload: dict) -> None:
        """Async :meth:`put`: the prune, the payload dumps and both commits off-loop."""
        await asyncio.to_thread(self.put, request_id, payload)

    async def aget(self, request_id: str) -> dict | None:
        """Async :meth:`get`: a status-poll read must not park the loop."""
        return await asyncio.to_thread(self.get, request_id)

    async def apayloads(self) -> list[JsonValue]:
        """Async :meth:`payloads`: a whole-cache rollup must not park the loop."""
        return await asyncio.to_thread(self.payloads)

    def _prune(self, now: float) -> None:
        if self._ttl_seconds <= 0:
            return
        self._conn.execute("DELETE FROM results WHERE stored_at < ?", (now - self._ttl_seconds,))
