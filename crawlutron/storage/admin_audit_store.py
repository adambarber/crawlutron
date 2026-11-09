"""The append-only admin audit trail: one durable row per key-admin action.

Every mint, revoke, inventory read, and failed operator guess lands here as
structured fields only -- actor, action, target key id, timestamp, outcome.
No secret material ever enters a row (#660 redaction law): the row has no
free-text column by construction. Rows are never rewritten; retention is a
bounded roll applied at the append itself, so a mint flood cannot grow the
table past its cap and a read never pays for the sweep.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations.manifest import StoreSchema

#: The trail's size bound. Key-admin actions are operator-paced (the keys door
#: meters guesses, mints are operator-initiated), so the population is bounded
#: by admin activity, not by the keyed-caller count; 10k rows at ~150 bytes is
#: comfortably under a megabyte while spanning months of normal operation.
DEFAULT_MAX_EVENTS = 10_000

#: Age bound in seconds: 30 days. The roll drops lapsed rows at the next
#: append; a quiet deployment's trail stays bounded by the age window alone.
RETENTION_SECONDS = 30 * 24 * 3600.0


class AdminAuditEvent(BaseModel):
    """One durable admin action: structured fields, no free text."""

    sequence: int
    recorded_at: float
    actor: str
    action: str
    target_key_id: str | None = None
    outcome: str = "granted"


class AdminAuditStore(CoordinationStore):
    """SQLite-backed append-only store of key-admin events, sequence-ordered."""

    schema = StoreSchema.KEY_ADMIN_EVENTS

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] | None = None,
        max_events: int = DEFAULT_MAX_EVENTS,
        retention_seconds: float = RETENTION_SECONDS,
    ) -> None:
        super().__init__(db_path)
        self._max_events = max(1, int(max_events))
        self._retention_seconds = max(0.0, float(retention_seconds))
        self._clock = clock or time.time

    def record_event(self, *, actor: str, action: str, target_key_id: str | None, outcome: str) -> None:
        """Append one event, then roll retention: the append is the roll point."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO key_admin_events (sequence, recorded_at, actor, action,"
                " target_key_id, outcome)"
                " VALUES ((SELECT COALESCE(MAX(sequence), 0) + 1 FROM key_admin_events),"
                " ?, ?, ?, ?, ?)",
                (self._clock(), actor, action, target_key_id, outcome),
            )
            self._roll()
            self._conn.commit()

    def list_events(self, *, limit: int | None = None) -> list[AdminAuditEvent]:
        """The retained trail, newest first; a bounded page by default."""
        query = (
            "SELECT sequence, recorded_at, actor, action, target_key_id, outcome"
            " FROM key_admin_events ORDER BY sequence DESC"
        )
        with self._lock:
            cursor = (
                self._conn.execute(query + " LIMIT ?", (limit,))
                if limit is not None
                else self._conn.execute(query)
            )
            rows = cursor.fetchall()
        return [AdminAuditEvent(**dict(zip(_COLUMNS, row, strict=False))) for row in rows]

    def _roll(self) -> None:
        """Drop rows past the age window or beyond the cap, inside the append."""
        now = self._clock()
        if self._retention_seconds > 0:
            self._conn.execute(
                "DELETE FROM key_admin_events WHERE recorded_at < ?",
                (now - self._retention_seconds,),
            )
        self._conn.execute(
            "DELETE FROM key_admin_events WHERE sequence NOT IN"
            " (SELECT sequence FROM key_admin_events ORDER BY sequence DESC LIMIT ?)",
            (self._max_events,),
        )

    arecord_event = atwin(record_event)
    alist_events = atwin(list_events)


_COLUMNS = ("sequence", "recorded_at", "actor", "action", "target_key_id", "outcome")

__all__ = ["AdminAuditEvent", "AdminAuditStore"]
