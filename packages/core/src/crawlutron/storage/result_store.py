"""Persist completed single-request crawl results for later retrieval, fixing the
202 webhook's missing ``status_url`` and the crawl work lost on a dropped
connection. Payloads store keyed by ``request_id`` with TTL + count + byte guardrails.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from safe_url.host_policy import HostPolicy

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.json_value import JsonValue
from crawlutron.models.serialization import from_wire, to_dict
from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations import StoreSchema
from crawlutron.storage.safe_db import txn_write
from crawlutron.transport.freshness import default_clock

# The write-edge byte cap: one retained row is a whole crawl response, so
# without a byte bound the entry cap bounds rows, never retained bytes.
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


class PayloadTooLargeError(ValueError):
    """A serialized payload past the store's byte cap, refused before persistence."""


class _PayloadStoreGuardrails(CoordinationStore):
    """The guardrails both payload stores share: bounded byte cap, defaulted clock.
    Abstract intermediate: a concrete subclass declares ``schema``.
    """

    _max_payload_bytes: int
    _clock: Callable[[], float]
    # The live-writer fence: spill writers set it, the sweep reads it. A store
    # that never fences (the results store) holds the empty set, unread.
    _live_requests: set[str]

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(db_path)
        self._max_payload_bytes = max(1, int(max_payload_bytes))
        self._clock = clock or default_clock
        self._live_requests = set()


class CrawlResultStore(_PayloadStoreGuardrails):
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
        **guardrails,
    ) -> None:
        """``max_payload_bytes`` and ``clock`` ride the shared guardrails base."""
        super().__init__(db_path, **guardrails)
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))

    @property
    def ttl_seconds(self) -> float:
        """The retention vocabulary: one home, so the spill sweep derives from it."""
        return self._ttl_seconds

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
            with txn_write(self._conn, owner="result_store.put"):
                if self._ttl_seconds > 0:
                    self._conn.execute(
                        "DELETE FROM results WHERE stored_at < ?",
                        (stored_at - self._ttl_seconds,),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO results (request_id, stored_at, payload, owner) "
                    "VALUES (?, ?, ?, ?)",
                    (request_id, stored_at, blob, owner),
                )
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
                        " WHERE stored_at >= ? AND (stored_at < ? OR (stored_at = ? AND "
                        "request_id < ?))"
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

    def delete(self, request_id: str) -> bool:
        """Drop a stored result row so a deleted job's content is not served past TTL.
        Returns True iff a live row matched; ownership established upstream."""
        with self._lock:
            with txn_write(self._conn, owner="result_store.delete"):
                rowcount = self._conn.execute(
                    "DELETE FROM results WHERE request_id = ?", (request_id,)
                ).rowcount
        return rowcount == 1

    # One ``atwin`` twin per operation: the sync signature is declared once and
    # ``owner=`` rides through **kwargs, so the twins cannot drift from the base.
    aput = atwin(put)
    aget = atwin(get)
    aget_for_caller = atwin(get_for_caller)
    adelete = atwin(delete)
    apayloads_page = atwin(payloads_page)
    aretained_count = atwin(retained_count)


class CrawlSpillStore(_PayloadStoreGuardrails):
    """SQLite spill of page results keyed by (request_id, sequence): the engine
    writes one row per result, reassembly is one ordered SELECT, reclamation a
    DELETE by age; every ``put`` fences its request_id as a live writer.
    """

    schema = StoreSchema.CRAWL_SPILLS

    def __init__(
        self,
        db_path: str | Path,
        *,
        lease_ttl_seconds: float = 300.0,
        **guardrails,
    ) -> None:
        super().__init__(db_path, **guardrails)
        self._lease_ttl_seconds = max(0.0, float(lease_ttl_seconds))

    def put(self, request_id: str, sequence: int, result: EngineCrawlResult) -> None:
        """Store one page result at its submission index; the encode-and-dump is
        this method's whole body, so the atwin runs the multi-MB walk off the loop.
        """
        blob = json.dumps({"i": sequence, "r": to_dict(result)})
        if len(blob) > self._max_payload_bytes:
            raise PayloadTooLargeError(
                f"serialized payload is {len(blob)} bytes; the cap is {self._max_payload_bytes}"
            )
        now = self._clock()
        expires_at = now + self._lease_ttl_seconds
        with self._lock:
            with txn_write(self._conn, owner="result_store.spill_put"):
                self._conn.execute(
                    "INSERT INTO crawl_spill_leases (request_id, expires_at) VALUES (?, ?) "
                    "ON CONFLICT(request_id) DO UPDATE SET expires_at = excluded.expires_at",
                    (request_id, expires_at),
                )
                self._conn.execute(
                    "INSERT INTO crawl_spills (request_id, sequence, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (request_id, sequence, blob, now),
                )

    def load_ordered(self, request_id: str, *, policy: HostPolicy) -> list[EngineCrawlResult]:
        """Reassemble one crawl's spilled results in submission order.

        Degradation ruling (supersedes the deleted file spill's corrupt-line
        skip, #C8): a corrupt PAYLOAD row RAISES, never silently N-1 of N."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM crawl_spills WHERE request_id = ? ORDER BY sequence",
                (request_id,),
            ).fetchall()
        return [from_wire(EngineCrawlResult, json.loads(blob)["r"], policy=policy) for (blob,) in rows]

    def release_spill(self, request_id: str) -> None:
        """The writer's release: this request's rows are served or its crawl has
        ended, so they are age-reclaimable again. The fence lives only for the
        crawl's lifetime, so a dead crawl's rows cannot be fenced forever."""
        with self._lock:
            with txn_write(self._conn, owner="result_store.spill_release"):
                self._conn.execute("DELETE FROM crawl_spill_leases WHERE request_id = ?", (request_id,))

    def gc_stale_spills(self, max_age_seconds: float) -> int:
        """Delete rows older than ``max_age_seconds`` EXCEPT a live writer's; the
        return is the row count reclaimed. The fence is shared DB state so any
        worker sees active leases.
        """
        now = self._clock()
        cutoff = now - max(0.0, max_age_seconds)
        with self._lock:
            with txn_write(self._conn, owner="result_store.gc_stale_spills"):
                self._conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _spill_fenced (request_id TEXT PRIMARY KEY)"
                )
                self._conn.execute("DELETE FROM _spill_fenced")
                self._conn.execute(
                    "INSERT INTO _spill_fenced (request_id) "
                    "SELECT request_id FROM crawl_spill_leases WHERE expires_at > ?",
                    (now,),
                )
                rowcount = self._conn.execute(
                    "DELETE FROM crawl_spills WHERE created_at < ? "
                    "AND request_id NOT IN (SELECT request_id FROM _spill_fenced)",
                    (cutoff,),
                ).rowcount
        return rowcount

    aput = atwin(put)
    aload_ordered = atwin(load_ordered)
    agc_stale_spills = atwin(gc_stale_spills)
    arelease_spill = atwin(release_spill)
