"""Cross-process shared counters -- the substrate `/metrics` aggregates from,
because a ``threading.Lock`` is invisible to sibling workers. An increment is a
single SQLite upsert; counters are not per-process summed at read time.
"""

from __future__ import annotations

import math

from app.storage.coordination_store import CoordinationStore
from app.storage.migrations.manifest import StoreSchema

__all__ = ["CoordinationCounters"]


class CoordinationCounters(CoordinationStore):
    """Named float totals shared by every worker process.

    Counter names arrive from metric labels, so every name is a bound
    parameter. There is no path in this class that interpolates one into SQL.
    """

    schema = StoreSchema.COORDINATION_COUNTERS

    def add(self, name: str, delta: float = 1.0) -> None:
        """Add ``delta`` to ``name``, creating the counter at zero if new.
        Rejects a non-finite delta: SQLite stores NaN as NULL, which would
        read as zero forever -- a flattened series no later write recovers.
        """
        value = float(delta)
        if not math.isfinite(value):
            raise ValueError(f"counter delta must be finite, got {delta!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO coordination_counters (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
                (name, value),
            )
            self._conn.commit()

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
        """Every recorded name and total, for the aggregating read path."""
        with self._lock:
            rows = self._conn.execute("SELECT name, value FROM coordination_counters").fetchall()
        return {str(name): float(value) for name, value in rows}

    def reset(self) -> None:
        """Drop every counter. Test isolation and operator reset."""
        with self._lock:
            self._conn.execute("DELETE FROM coordination_counters")
            self._conn.commit()
