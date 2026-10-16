"""Durable watch registry: one row per URL to revalidate on a schedule. The store
is mechanical -- the caller makes every decision a row encodes -- and ``due`` is
read-only, so a died sweep leaves the watch due and checks run at-least-once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models.content_validators import ContentValidators
from app.models.normalized_url import NormalizedUrl
from app.models.watch_outcome import WatchOutcome
from app.models.watch_state import WatchState
from app.models.watch_status import WatchStatus
from app.storage.coordination_store import CoordinationStore, atwin
from app.storage.ids import new_watch_id
from app.storage.migrations import StoreSchema

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
    ) -> WatchState:
        """Register a watch, scheduled for its first check one interval out.

        The state model is the invariant gate: a non-positive interval
        cannot become a row here or be observed anywhere downstream.
        """
        state = WatchState(
            watch_id=new_watch_id(),
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
                    watch_id, url, webhook_url, interval_seconds, status,
                    validators, next_check_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.watch_id,
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

    def list_watches(self) -> list[WatchState]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM watches ORDER BY created_at, watch_id").fetchall()
        return [_row_to_state(row) for row in rows]

    def delete(self, watch_id: str) -> bool:
        """Remove a watch; ``False`` means no such watch existed."""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM watches WHERE watch_id = ?", (watch_id,))
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

    def record_check(
        self,
        watch_id: str,
        *,
        outcome: WatchOutcome,
        now: float,
        next_check_at: float,
        validators: ContentValidators,
    ) -> None:
        """Persist a completed check's result and reschedule. The caller hands back the
        effective validators (200 → the origin's; 304/error → the stored ones), so the
        row's set is a decision, not a ``COALESCE`` that could keep a dropped validator.
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE watches SET
                    last_checked_at = ?,
                    last_outcome = ?,
                    -- a modified check moves the change clock; nothing else does
                    last_changed_at = CASE WHEN ? = 'modified' THEN ? ELSE last_changed_at END,
                    -- an error extends the failure streak; any other outcome resets it
                    consecutive_failures = CASE WHEN ? = 'error' THEN consecutive_failures + 1 ELSE 0 END,
                    validators = ?,
                    next_check_at = ?,
                    updated_at = ?
                WHERE watch_id = ?
                """,
                (
                    now,
                    outcome,
                    outcome,
                    now,
                    outcome,
                    validators.model_dump_json(exclude_none=True),
                    next_check_at,
                    now,
                    watch_id,
                ),
            )
            self._conn.commit()

    # One ``atwin`` twin per operation: the signature is declared once, by the
    # sync method, and an async caller never holds the loop while the lock is.

    acreate = atwin(create)
    arecord_check = atwin(record_check)
    aget = atwin(get)
    alist_watches = atwin(list_watches)
    adelete = atwin(delete)
    adue = atwin(due)


def _row_to_state(row: sqlite3.Row) -> WatchState:
    """Named-value construction: a missing column is an error, not a default."""
    return WatchState(
        watch_id=row["watch_id"],
        url=row["url"],
        webhook_url=row["webhook_url"],
        interval_seconds=row["interval_seconds"],
        status=WatchStatus(row["status"]),
        validators=ContentValidators.model_validate_json(row["validators"]),
        next_check_at=row["next_check_at"],
        last_checked_at=row["last_checked_at"],
        last_outcome=WatchOutcome(row["last_outcome"]) if row["last_outcome"] else None,
        last_changed_at=row["last_changed_at"],
        consecutive_failures=row["consecutive_failures"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
