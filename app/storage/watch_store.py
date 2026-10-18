"""Durable watch registry: one row per URL to revalidate on a schedule. The store
is mechanical -- the caller makes every decision a row encodes -- and ``due`` is
read-only, so a died sweep leaves the watch due and checks run at-least-once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models.content_validators import ContentValidators
from app.storage.coordination_store import CoordinationStore, atwin
from app.storage.ids import new_watch_id
from app.storage.migrations import StoreSchema
from app.storage.watch_surface import NormalizedUrl, WatchOutcome, WatchState, WatchStatus

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

    def record_check(
        self,
        watch_id: str,
        *,
        outcome: WatchOutcome,
        now: float,
        next_check_at: float,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Persist a completed check's result and reschedule. ``etag``/``last_modified``
        are stored only on a ``modified`` outcome; other verdicts keep the stored set.
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
                    -- a delivered body replaces the set outright; other verdicts keep it
                    validators = CASE WHEN ? = 'modified' THEN ? ELSE validators END,
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
                    outcome,
                    ContentValidators(
                        etag=etag if outcome is WatchOutcome.MODIFIED else None,
                        last_modified=(last_modified if outcome is WatchOutcome.MODIFIED else None),
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
                    updated_at = ?
                WHERE watch_id = ?
                """,
                (WatchStatus.DEACTIVATED, WatchOutcome.ERROR, next_check_at, now, watch_id),
            )
            self._conn.commit()

    # One ``atwin`` twin per operation: the signature is declared once, by the
    # sync method, and an async caller never holds the loop while the lock is.

    acreate = atwin(create)
    arecord_check = atwin(record_check)
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
        last_outcome=WatchOutcome(row["last_outcome"]) if row["last_outcome"] else None,
        last_changed_at=row["last_changed_at"],
        consecutive_failures=row["consecutive_failures"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
