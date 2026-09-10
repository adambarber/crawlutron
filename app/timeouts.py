"""Timeout budgets chokepoint: one named place for HTTP timeout numbers.

``httpx.Timeout`` was constructed at four call sites, each inventing its
own connect/read/write/pool values -- and one site omitted ``write``,
which httpx rejects at runtime. Every new I/O site picks a named
constructor here instead of inventing numbers:

- :meth:`Timeouts.for_request` -- per-request budgets derived from the
  caller's ``timeout_seconds`` (fetch paths, search providers).
- :meth:`Timeouts.defaults` -- service posture for shared/long-lived
  clients (the pooled HTTP client, webhook delivery).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeouts:
    """Named connect/read/write/pool budgets (seconds)."""

    connect: float = 5.0
    read: float = 30.0
    write: float = 10.0
    pool: float = 5.0

    @classmethod
    def for_request(cls, read_seconds: float) -> Timeouts:
        """Per-request budgets: the caller's read budget, sane connect/write/pool."""
        return cls(connect=5.0, read=float(read_seconds), write=10.0, pool=5.0)

    @classmethod
    def defaults(cls) -> Timeouts:
        """Service posture for shared clients (30s read across the board)."""
        return cls()

    def as_httpx(self):
        """Render as ``httpx.Timeout`` (all four parameters, always valid)."""
        import httpx

        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )
