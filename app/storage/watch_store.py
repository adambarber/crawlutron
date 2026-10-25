"""Durable watch registry: one row per URL to revalidate on a schedule. The store
is mechanical -- the caller makes every decision a row encodes -- and ``due`` is
read-only, so a died sweep leaves the watch due and checks run at-least-once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models.content_validators import ContentValidators
from app.models.normalized_url import NormalizedUrl
from app.models.watch_state import WatchState
from app.models.watch_status import WatchStatus
from app.storage.coordination_store import CoordinationStore, atwin
from app.storage.ids import new_watch_id
from app.storage.migrations import StoreSchema
from app.vocabulary.taxonomy import (
    CrawlClassification,
    status_for_classification,
)

__all__ = ["WatchStore"]


class WatchStore(CoordinationStore):
    """SQLite-backed registry of watches. The connection, PRAGMAs, lock, and
    ``close`` are the base's (it needs nothing beyond a path); it owns no policy or
    tier -- a watch arrives already admitted and the row stores text identity.
    """

    schema = StoreSchema.WATCHES

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(db_path)
        # Column-addressed rows: the schema grows by migration, so positional
        # indexing would couple the read path to ALTER ordering.
        self._conn.row_factory = sqlite3.Row

    def create(
        self,
        url: NormalizedUrl,
        /,
        *,
        interval_seconds: int,
        now: float,
        webhook_url: str | None = None,
        owner: str = "",
    ) -> WatchState:
        """Register a watch, scheduled for its first check one interval out. The
        state model gates a non-positive interval; ``owner`` is the anonymity
        bucket ("") or the keyed caller's id.
        """
        state = WatchState(
            watch_id=new_watch_id(),
            owner=owner,
            url=url.raw,
            webhook_url=webhook_url,
            interval_seconds=interval_seconds,
            next_check_at=now + interval_seconds,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO watches (
                    watch_id, owner, url, webhook_url, interval_seconds, status,
                    validators, next_check_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.watch_id,
                    state.owner,
                    state.url,
                    state.webhook_url,
                    state.interval_seconds,
                    state.status,
                    state.validators.model_dump_json(exclude_none=True),
                    state.next_check_at,
                    state.created_at,
                    state.updated_at,
                ),
            )
            self._conn.commit()
        return state

    def get(self, watch_id: str) -> WatchState | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM watches WHERE watch_id = ?", (watch_id,)).fetchone()
        return None if row is None else _row_to_state(row)

    def get_for_caller(self, watch_id: str, caller_id: str) -> WatchState | None:
        """The watch iff ``caller_id`` owns it; a foreign row reads the same miss."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watches WHERE watch_id = ? AND owner = ?", (watch_id, caller_id)
            ).fetchone()
        return None if row is None else _row_to_state(row)

    def list_watches(self) -> list[WatchState]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM watches ORDER BY created_at, watch_id").fetchall()
        return [_row_to_state(row) for row in rows]

    def list_for_caller(self, caller_id: str) -> list[WatchState]:
        """The caller's own watches; foreign rows are not listed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watches WHERE owner = ? ORDER BY created_at, watch_id", (caller_id,)
            ).fetchall()
        return [_row_to_state(row) for row in rows]

    def count_for_owner(self, owner: str) -> int:
        """How many watches ``owner`` has registered; the per-caller cap reads it."""
        with self._lock:
            row = self._conn.execute("SELECT count(*) FROM watches WHERE owner = ?", (owner,)).fetchone()
        return int(row[0])

    def delete(self, watch_id: str) -> bool:
        """Remove a watch; ``False`` means no such watch existed."""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM watches WHERE watch_id = ?", (watch_id,))
            self._conn.commit()
        return cursor.rowcount == 1

    def delete_for_caller(self, watch_id: str, caller_id: str) -> bool:
        """Remove ``watch_id`` iff ``caller_id`` owns it; a foreign id deletes nothing."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM watches WHERE watch_id = ? AND owner = ?", (watch_id, caller_id)
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def due(self, now: float) -> list[WatchState]:
        """Active watches whose next check is at or before ``now``, earliest first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM watches
                WHERE status = ? AND next_check_at <= ?
                ORDER BY next_check_at
                """,
                (WatchStatus.ACTIVE, now),
            ).fetchall()
        return [_row_to_state(row) for row in rows]

    def claim_due(self, now: float, token: str, *, claim_seconds: float) -> list[WatchState]:
        """Atomically claim the due watches for one sweep pass: an UPDATE stamps
        ``token``/``claimed_at`` on every due, unclaimed (or stale-claimed) row and
        the claimed rows are returned; a second sweeper's CAS loses and skips (#528).
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE watches SET claimed_by = ?, claimed_at = ?
                WHERE status = ? AND next_check_at <= ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (token, now, WatchStatus.ACTIVE, now, now - claim_seconds),
            )
            self._conn.commit()
            rows = self._conn.execute(
                """
                SELECT * FROM watches
                WHERE status = ? AND next_check_at <= ? AND claimed_by = ?
                ORDER BY next_check_at
                """,
                (WatchStatus.ACTIVE, now, token),
            ).fetchall()
        return [_row_to_state(row) for row in rows]

    def record_check(
        self,
        watch_id: str,
        *,
        outcome: CrawlClassification,
        now: float,
        next_check_at: float,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Persist a completed check's result and reschedule. ``etag``/``last_modified``
        are stored only on a delivered change; other verdicts keep the stored set.
        A ``skipped`` (robots refusal) is a non-attempt, not an error (#335).
        """
        status = status_for_classification(outcome)
        modified = status == "success" and outcome != CrawlClassification.not_modified
        error = status not in ("success", "skipped")
        with self._lock:
            self._conn.execute(
                """
                UPDATE watches SET
                    last_checked_at = ?,
                    last_outcome = ?,
                    -- a delivered change moves the change clock; nothing else does
                    last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
                    -- a failure check extends the streak; any other verdict resets it
                    consecutive_failures = CASE WHEN ? THEN consecutive_failures + 1 ELSE 0 END,
                    -- a delivered body replaces the set outright; other verdicts keep it
                    validators = CASE WHEN ? THEN ? ELSE validators END,
                    next_check_at = ?,
                    -- the claim ends with the check: a completed row is unclaimed
                    claimed_at = NULL,
                    updated_at = ?
                WHERE watch_id = ?
                """,
                (
                    now,
                    outcome,
                    modified,
                    now,
                    error,
                    modified,
                    ContentValidators(
                        etag=etag if modified else None,
                        last_modified=(last_modified if modified else None),
                    ).model_dump_json(exclude_none=True),
                    next_check_at,
                    now,
                    watch_id,
                ),
            )
            self._conn.commit()

    def deactivate(self, watch_id: str, *, now: float, next_check_at: float) -> None:
        """Stop a watch being checked: terminal status plus a reschedule far out.
        Used when the owner's key was revoked, so a dead identity cannot issue
        checks forever; the row and its history stay for the operator to read.
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE watches SET
                    status = ?,
                    last_outcome = ?,
                    next_check_at = ?,
                    -- the claim ends with the check: a completed row is unclaimed
                    claimed_at = NULL,
                    claimed_by = NULL,
                    updated_at = ?
                WHERE watch_id = ?
                """,
                (WatchStatus.DEACTIVATED, CrawlClassification.site_error, next_check_at, now, watch_id),
            )
            self._conn.commit()

    # One ``atwin`` twin per operation: the signature is declared once, by the
    # sync method, and an async caller never holds the loop while the lock is.

    acreate = atwin(create)
    arecord_check = atwin(record_check)
    aclaim_due = atwin(claim_due)
    adeactivate = atwin(deactivate)
    aget = atwin(get)
    aget_for_caller = atwin(get_for_caller)
    alist_watches = atwin(list_watches)
    alist_for_caller = atwin(list_for_caller)
    acount_for_owner = atwin(count_for_owner)
    adelete = atwin(delete)
    adelete_for_caller = atwin(delete_for_caller)
    adue = atwin(due)


def _row_to_state(row: sqlite3.Row) -> WatchState:
    """Named-value construction: a missing column is an error, not a default."""
    return WatchState(
        watch_id=row["watch_id"],
        owner=row["owner"],
        url=row["url"],
        webhook_url=row["webhook_url"],
        interval_seconds=row["interval_seconds"],
        status=WatchStatus(row["status"]),
        validators=ContentValidators.model_validate_json(row["validators"]),
        next_check_at=row["next_check_at"],
        last_checked_at=row["last_checked_at"],
        last_outcome=CrawlClassification(row["last_outcome"]) if row["last_outcome"] else None,
        last_changed_at=row["last_changed_at"],
        consecutive_failures=row["consecutive_failures"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
