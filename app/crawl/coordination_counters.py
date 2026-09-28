"""Cross-process shared counters -- the substrate `/metrics` aggregates from.

`MetricsStore` kept its counters in a dataclass singleton guarded by a
`threading.Lock`. A `threading.Lock` is invisible to every process but the one
holding it, so with `uvicorn --workers` the endpoint served one worker's
numbers and called the result total traffic. The same shape had already cost
the service once: content-cache spill GC held its live-spill set in the
process, so a sibling worker's in-flight spill was indistinguishable from one
abandoned by a killed run and was unlinked mid-crawl.

A SQLite table is the fix that needs no new dependency and no new coordination
protocol. The increment is a single upsert naming the column in its SET list,
so it is a read-modify-write inside one SQLite transaction: two processes
racing on the same name serialize at the database's own lock rather than at a
Python object neither of them can see. WAL plus the store chokepoint's
`busy_timeout` is what keeps a contended write waiting instead of raising.

Counters are deliberately NOT per-process tallies summed at read time. A
worker's contribution cannot be attributed after the fact, and a worker that
dies would take its tallies with it; a shared total survives both.
"""

from __future__ import annotations

import math

from app.crawl.coordination_store import CoordinationStore
from app.crawl.migrations.manifest import StoreSchema

__all__ = ["CoordinationCounters"]


class CoordinationCounters(CoordinationStore):
    """Named float totals shared by every worker process.

    Counter names arrive from metric labels, so every name is a bound
    parameter. There is no path in this class that interpolates one into SQL.
    """

    schema = StoreSchema.COORDINATION_COUNTERS

    def add(self, name: str, delta: float = 1.0) -> None:
        """Add ``delta`` to ``name``, creating the counter at zero if new.

        Rejects a non-finite delta. SQLite stores NaN as NULL, which would turn
        the counter into a missing row and read as zero for the rest of the
        process's life -- a silently flattened series that no later write can
        recover from.
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
