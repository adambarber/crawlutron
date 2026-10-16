"""Persist completed single-request crawl results for later retrieval, fixing the
202 webhook's missing ``status_url`` and the crawl work lost on a dropped
connection. Payloads store keyed by ``request_id`` with TTL + count guardrails.
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
    """SQLite-backed store of completed crawl responses keyed by request id. The
    connection, PRAGMAs, lock, and ``close`` are the base's; this store once
    opened its own, drifting from the shared WAL config and duplicating ``close``.
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

    def put(self, request_id: str, payload: JsonValue, *, owner: str = "") -> None:
        """Store a completed response; prune expired/oldest entries first."""
        stored_at = self._clock()
        with self._lock:
            self._prune(stored_at)
            self._conn.execute(
                "INSERT OR REPLACE INTO results (request_id, stored_at, payload, owner) VALUES (?, ?, ?, ?)",
                (request_id, stored_at, json.dumps(payload), owner),
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

    def get(self, request_id: str) -> JsonValue | None:
        """Return the stored payload, or None if absent/expired."""
        return self._read_owned(request_id, None)

    def get_for_caller(self, request_id: str, caller_id: str) -> JsonValue | None:
        """The payload iff ``caller_id`` owns it; foreign rows read as missing."""
        return self._read_owned(request_id, caller_id)

    def _read_owned(self, request_id: str, caller_id: str | None) -> JsonValue | None:
        """The payload, filtered to ``caller_id`` when one is given."""
        now = self._clock()
        cutoff = now - self._ttl_seconds if self._ttl_seconds > 0 else 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT stored_at, payload, owner FROM results WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        stored_at, payload, owner = row
        if caller_id is not None and owner != caller_id:
            return None
        if self._ttl_seconds > 0 and stored_at < cutoff:
            return None
        return json.loads(payload)

    def payloads(self) -> list[JsonValue]:
        """Every retained payload, newest first -- the same TTL filter as :meth:`get`,
        so an expired row cannot report evidence the store has declared gone.
        Newest-first lets a caller reading a bounded prefix see the recent records.
        """
        now = self._clock()
        cutoff = now - self._ttl_seconds if self._ttl_seconds > 0 else 0.0
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM results WHERE stored_at >= ? ORDER BY stored_at DESC",
                (cutoff,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def aput(self, request_id: str, payload: JsonValue, *, owner: str = "") -> None:
        """Async :meth:`put`: the prune, the payload dumps and both commits off-loop."""
        await asyncio.to_thread(self.put, request_id, payload, owner=owner)

    async def aget(self, request_id: str) -> JsonValue | None:
        """Async :meth:`get`: a status-poll read must not park the loop."""
        return await asyncio.to_thread(self.get, request_id)

    async def aget_for_caller(self, request_id: str, caller_id: str) -> JsonValue | None:
        """Async :meth:`get_for_caller`: a scoped poll read stays off the loop."""
        return await asyncio.to_thread(self.get_for_caller, request_id, caller_id)

    async def apayloads(self) -> list[JsonValue]:
        """Async :meth:`payloads`: a whole-cache rollup must not park the loop."""
        return await asyncio.to_thread(self.payloads)

    def _prune(self, now: float) -> None:
        if self._ttl_seconds <= 0:
            return
        self._conn.execute("DELETE FROM results WHERE stored_at < ?", (now - self._ttl_seconds,))
