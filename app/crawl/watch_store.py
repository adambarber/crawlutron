"""Durable watch registry: one row per URL to revalidate on a schedule.

The store is mechanical. Every decision a row encodes -- the interval, the
effective validators after a check, whether a failure counts -- is made by
the caller (the registration edge or the sweep) and written here, so the
store can never disagree with the sweep about what a check meant.

``due`` is read-only: the sweep claims a watch by recording its result. A
sweep that dies between ``due`` and ``record_check`` leaves the watch due,
so checks run at-least-once rather than being silently skipped -- the
honest default for a revalidator. The documented escape hatch for a
multi-process deployment is an atomic claim ``UPDATE ... WHERE
next_check_at = ?`` added to ``due``; a single-writer sweep (this service's
model) has no race to protect against, so the claim dance is not paid for
up front.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from app.crawl.coordination_store import CoordinationStore
from app.crawl.ids import new_watch_id
from app.crawl.migrations import StoreSchema
from app.models.content_validators import ContentValidators
from app.models.normalized_url import NormalizedUrl
from app.models.watch_outcome import WatchOutcome
from app.models.watch_state import WatchState
from app.models.watch_status import WatchStatus

__all__ = ["WatchStore"]


class WatchStore(CoordinationStore):
    """SQLite-backed registry of watches.

    The connection, its PRAGMAs, the intra-process lock and ``close`` are the
    base's, exactly as they are for the other three coordination stores. A watch
    needs nothing beyond a path, so it is the base's shape precisely -- and
    writing a second ``__init__`` here is what already cost ``CrawlResultStore``
    its ``close``.

    It owns no policy or tier: a watch arrives already admitted
    (``NormalizedUrl`` at the edge) and the row stores text identity, so a later
    deployment re-admits against its own live host policy rather than trusting a
    decision a past one made.
    """

    schema = StoreSchema.WATCHES

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(db_path)
        # Column-addressed rows, for JobStore's reason: the schema grows by
        # migration, so positional indexing for a later-added column would couple
        # the read path to ALTER ordering. sqlite3.Row still supports row[n].
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
        """Persist a completed check's result and reschedule.

        The caller hands back the effective validators (a 200 means the
        ones the origin returned; a 304 or an error means the ones already
        stored), so the row's validator set is a decision, not a merge --
        there is no COALESCE here that could quietly keep a validator the
        origin dropped.
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

    # One async twin per operation, each the same process lock the sync call
    # takes -- so an async caller (the sweep, the API) never holds the event
    # loop while that lock is held, and a twin can never race a direct call.
    # Named as ``JobStore`` names its own, so both stores read the same way.

    async def acreate(
        self,
        url: NormalizedUrl,
        /,
        *,
        interval_seconds: int,
        now: float,
        webhook_url: str | None = None,
    ) -> WatchState:
        """Async form of :meth:`create`."""
        return await asyncio.to_thread(
            self.create,
            url,
            interval_seconds=interval_seconds,
            now=now,
            webhook_url=webhook_url,
        )

    async def aget(self, watch_id: str) -> WatchState | None:
        """Async form of :meth:`get`."""
        return await asyncio.to_thread(self.get, watch_id)

    async def alist_watches(self) -> list[WatchState]:
        """Async form of :meth:`list_watches`."""
        return await asyncio.to_thread(self.list_watches)

    async def adelete(self, watch_id: str) -> bool:
        """Async form of :meth:`delete`."""
        return await asyncio.to_thread(self.delete, watch_id)

    async def adue(self, now: float) -> list[WatchState]:
        """Async form of :meth:`due`."""
        return await asyncio.to_thread(self.due, now)

    async def arecord_check(
        self,
        watch_id: str,
        *,
        outcome: WatchOutcome,
        now: float,
        next_check_at: float,
        validators: ContentValidators,
    ) -> None:
        """Async form of :meth:`record_check`."""
        await asyncio.to_thread(
            self.record_check,
            watch_id,
            outcome=outcome,
            now=now,
            next_check_at=next_check_at,
            validators=validators,
        )


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
