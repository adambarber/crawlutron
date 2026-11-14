"""Cross-process shared counters -- the substrate `/metrics` aggregates from,
because a ``threading.Lock`` is invisible to sibling workers. An increment is a
single SQLite upsert; counters are not per-process summed at read time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations.manifest import StoreSchema
from crawlutron.storage.safe_db import txn_write

__all__ = ["CoordinationCounters"]


class CoordinationCounters(CoordinationStore):
    """Named float totals shared by every worker process.

    Counter names arrive from metric labels, so every name is a bound
    parameter. There is no path in this class that interpolates one into SQL.
    """

    schema = StoreSchema.COORDINATION_COUNTERS

    # Increments land here first so one crawl costs one transaction.
    _pending: dict[str, float] | None = None

    def add_pending(self, name: str, delta: float = 1.0) -> None:
        """Accumulate one increment; ``flush`` commits the batch."""
        value = float(delta)
        if not math.isfinite(value):
            raise ValueError(f"counter delta must be finite, got {delta!r}")
        with self._lock:
            pending = self._pending
            if pending is None:
                pending = self._pending = {}
            pending[name] = pending.get(name, 0.0) + value

    def flush(self) -> None:
        """Commit every pending increment in one transaction."""
        with self._lock:
            batch, self._pending = self._pending or {}, None
        self.add_many(batch)

    def add_many(self, deltas: Mapping[str, float]) -> None:
        """Apply every ``delta`` in one ``BEGIN IMMEDIATE … COMMIT``.

        A non-finite value is refused and nothing is written: the guard fires
        before any row touches the database, so no partial batch commits.
        """
        params: list[tuple[str, float]] = []
        for name, value in deltas.items():
            fv = float(value)
            if not math.isfinite(fv):
                raise ValueError(f"counter delta must be finite, got {value!r}")
            params.append((name, fv))
        if not params:
            return
        with self._lock:
            with txn_write(self._conn, owner="coordination_counters.add_many"):
                self._conn.executemany(
                    "INSERT INTO coordination_counters (name, value) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
                    params,
                )

    def add(self, name: str, delta: float = 1.0) -> None:
        """Add ``delta`` to ``name``, creating the counter at zero if new.

        For per-crawl batching use ``add_many``; this method commits once
        per call (legacy path kept for non-crawl counters).
        """
        self.add_many({name: delta})

    def get(self, name: str) -> float:
        """Return the recorded total, or ``0.0`` for a name never written.

        Zero rather than an error so `/metrics` can render a complete series
        for names only some workers have touched yet.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM coordination_counters WHERE name = ?", (name,)
            ).fetchone()
        return 0.0 if row is None else float(row[0])

    def snapshot(self) -> dict[str, float]:
        """Every recorded name and total, including increments not yet flushed."""
        with self._lock:
            rows = self._conn.execute("SELECT name, value FROM coordination_counters").fetchall()
            pending = dict(self._pending or {})
        merged = {str(name): float(value) for name, value in rows}
        for name, value in pending.items():
            merged[name] = merged.get(name, 0.0) + value
        return merged

    def reset(self) -> None:
        """Drop every counter. Test isolation and operator reset."""
        with self._lock:
            self._pending = None
            with txn_write(self._conn, owner="coordination_counters.reset"):
                self._conn.execute("DELETE FROM coordination_counters")

    aflush = atwin(flush)
    a_add_many = atwin(add_many)
    a_reset = atwin(reset)
