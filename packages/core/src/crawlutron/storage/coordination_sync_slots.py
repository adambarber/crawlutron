"""Durable sync-crawl slot rows shared by every worker process.

At ``CRAWLUTRON_WORKERS > 1`` a process-local counter cannot enforce the
per-caller ``max_concurrency`` quota; one coordination table does.
"""

from __future__ import annotations

import time

from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.ids import new_sync_slot_id
from crawlutron.storage.migrations.manifest import StoreSchema
from crawlutron.storage.safe_db import txn_read, txn_write

__all__ = ["CoordinationSyncSlotsStore"]

#: Staleness bound in seconds: a slot older than this is assumed abandoned.
#: Trade-off: a sync crawl running past the bound can be over-admitted once.
_STALENESS_SECONDS = 300.0


class CoordinationSyncSlotsStore(CoordinationStore):
    """Sync-crawl slot rows shared across all worker processes."""

    schema = StoreSchema.COORDINATION_SYNC_SLOTS

    def reserve(
        self,
        caller_id: str,
        max_concurrency: int,
        already_active: int = 0,
        now: float | None = None,
    ) -> str | None:
        """Reserve one slot atomically; ``None`` refuses an over-quota admit.

        The count, the stale purge, and the insert share one ``BEGIN IMMEDIATE``
        transaction, so SQLite's write lock is the cross-process admit mutex.
        """
        if max_concurrency == 0:
            return ""
        stamp = time.time() if now is None else now
        slot_id = f"{caller_id}:{new_sync_slot_id()}"
        with self._lock:
            with txn_write(self._conn, owner="coordination_sync_slots.reserve"):
                self._conn.execute(
                    "DELETE FROM coordination_sync_slots WHERE reserved_at <= ?",
                    (stamp - _STALENESS_SECONDS,),
                )
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM coordination_sync_slots WHERE caller_id = ?",
                    (caller_id,),
                ).fetchone()
                held = 0 if row is None else int(row[0])
                if already_active + held >= max_concurrency:
                    return None
                self._conn.execute(
                    "INSERT INTO coordination_sync_slots (caller_id, slot_id, reserved_at) VALUES (?, ?, ?)",
                    (caller_id, slot_id, stamp),
                )
                return slot_id

    def release(self, caller_id: str, slot_id: str) -> None:
        """Delete one reserved sync slot; the unbounded sentinel reserves nothing."""
        if not slot_id:
            return
        with self._lock:
            with txn_write(self._conn, owner="coordination_sync_slots.release"):
                self._conn.execute(
                    "DELETE FROM coordination_sync_slots WHERE caller_id = ? AND slot_id = ?",
                    (caller_id, slot_id),
                )

    def count_active(self, caller_id: str, now: float | None = None) -> int:
        """Non-stale reserved slots for ``caller_id`` at ``now``."""
        stamp = time.time() if now is None else now
        with self._lock:
            with txn_read(self._conn, owner="coordination_sync_slots.count_active"):
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM coordination_sync_slots WHERE caller_id = ? AND reserved_at > ?",
                    (caller_id, stamp - _STALENESS_SECONDS),
                ).fetchone()
        return 0 if row is None else int(row[0])

    def stale(self, cutoff: float) -> int:
        """Delete slots older than ``cutoff``; return the count deleted."""
        with self._lock:
            with txn_write(self._conn, owner="coordination_sync_slots.stale"):
                cursor = self._conn.execute(
                    "DELETE FROM coordination_sync_slots WHERE reserved_at <= ?",
                    (cutoff,),
                )
            return cursor.rowcount

    areserve = atwin(reserve)
    arelease = atwin(release)
    acount_active = atwin(count_active)
    astale = atwin(stale)
