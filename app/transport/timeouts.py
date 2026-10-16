"""Timeout budgets chokepoint: one named place for HTTP timeout numbers.

``for_request`` derives per-request budgets from the caller's ``timeout_seconds``;
``defaults`` is the service posture for shared/long-lived clients.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


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
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )
