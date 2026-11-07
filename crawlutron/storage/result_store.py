"""Persist completed single-request crawl results for later retrieval, fixing the
202 webhook's missing ``status_url`` and the crawl work lost on a dropped
connection. Payloads store keyed by ``request_id`` with TTL + count + byte guardrails.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from crawlutron.models.json_value import JsonValue
from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations import StoreSchema
from crawlutron.transport.freshness import default_clock

# The write-edge byte cap: one retained row is a whole crawl response, so
# without a byte bound the entry cap bounds rows, never retained bytes.
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


class PayloadTooLargeError(ValueError):
    """A serialized payload past the store's byte cap, refused before persistence."""


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
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(db_path)
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._max_payload_bytes = max(1, int(max_payload_bytes))
        self._clock = clock or default_clock

    def put(self, request_id: str, payload: JsonValue, *, owner: str = "") -> None:
        """Store a completed response; ``owner=""`` is the shared anonymous bucket."""
        # json.dumps defaults to ensure_ascii, so the dump is pure ASCII and its
        # length in characters IS its length in bytes: no second copy to measure.
        blob = json.dumps(payload)
        if len(blob) > self._max_payload_bytes:
            raise PayloadTooLargeError(
                f"serialized payload is {len(blob)} bytes; the cap is {self._max_payload_bytes}"
            )
        stored_at = self._clock()
        with self._lock:
            self._prune(stored_at)
            self._conn.execute(
                "INSERT OR REPLACE INTO results (request_id, stored_at, payload, owner) VALUES (?, ?, ?, ?)",
                (request_id, stored_at, blob, owner),
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
            if caller_id is None:
                row = self._conn.execute(
                    "SELECT stored_at, payload, owner FROM results WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT stored_at, payload, owner FROM results WHERE request_id = ? AND owner = ?",
                    (request_id, caller_id),
                ).fetchone()
        if row is None:
            return None
        stored_at, payload, _owner = row
        if self._ttl_seconds > 0 and stored_at < cutoff:
            return None
        return json.loads(payload)

    def _cutoff(self) -> float:
        now = self._clock()
        return now - self._ttl_seconds if self._ttl_seconds > 0 else 0.0

    def payloads_page(
        self, *, limit: int, cursor: tuple[float, str] | None = None, owner: str | None = None
    ) -> list[tuple[tuple[float, str], JsonValue]]:
        """One bounded page of retained payloads, newest first, with its
        (stored_at, request_id) continuation cursor; an empty page ends the scan.
        When ``owner`` is given, only rows owned by that caller are returned.
        """
        cutoff = self._cutoff()
        with self._lock:
            if cursor is None:
                if owner is None:
                    rows = self._conn.execute(
                        "SELECT stored_at, request_id, payload FROM results"
                        " WHERE stored_at >= ? ORDER BY stored_at DESC, request_id DESC LIMIT ?",
                        (cutoff, limit),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT stored_at, request_id, payload FROM results"
                        " WHERE stored_at >= ? AND owner = ?"
                        " ORDER BY stored_at DESC, request_id DESC LIMIT ?",
                        (cutoff, owner, limit),
                    ).fetchall()
            else:
                at, request_id = cursor
                if owner is None:
                    rows = self._conn.execute(
                        "SELECT stored_at, request_id, payload FROM results"
                        " WHERE stored_at >= ? AND (stored_at < ? OR (stored_at = ? AND request_id < ?))"
                        " ORDER BY stored_at DESC, request_id DESC LIMIT ?",
                        (cutoff, at, at, request_id, limit),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT stored_at, request_id, payload FROM results"
                        " WHERE stored_at >= ? AND owner = ?"
                        " AND (stored_at < ? OR (stored_at = ? AND request_id < ?))"
                        " ORDER BY stored_at DESC, request_id DESC LIMIT ?",
                        (cutoff, owner, at, at, request_id, limit),
                    ).fetchall()
        return [((stored_at, request_id), json.loads(payload)) for stored_at, request_id, payload in rows]

    def retained_count(self, *, owner: str | None = None) -> int:
        """Rows the TTL window still retains -- the rollup's honest denominator.
        When ``owner`` is given, only rows owned by that caller are counted.
        """
        cutoff = self._cutoff()
        with self._lock:
            if owner is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM results WHERE stored_at >= ?", (cutoff,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM results WHERE stored_at >= ? AND owner = ?",
                    (cutoff, owner),
                ).fetchone()
        return int(row[0]) if row is not None else 0

    # One ``atwin`` twin per operation: the sync signature is declared once and
    # ``owner=`` rides through **kwargs, so the twins cannot drift from the base.
    aput = atwin(put)
    aget = atwin(get)
    aget_for_caller = atwin(get_for_caller)
    apayloads_page = atwin(payloads_page)
    aretained_count = atwin(retained_count)

    def _prune(self, now: float) -> None:
        if self._ttl_seconds <= 0:
            return
        self._conn.execute("DELETE FROM results WHERE stored_at < ?", (now - self._ttl_seconds,))
