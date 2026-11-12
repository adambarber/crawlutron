"""Timeout budgets chokepoint: one named place for HTTP timeout numbers.

``for_request`` derives per-request budgets from the caller's ``timeout_seconds``;
``defaults`` is the service posture for shared/long-lived clients.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast

import httpx

from crawlutron.transport.render_page import RenderPage

#: The floor a render phase honors: a sub-second budget cannot bound a goto.
RENDER_PHASE_FLOOR_SECONDS = 1.0

#: The ceiling on one page read (evaluate or content): Playwright's reads carry
#: NO default deadline, so a wedged renderer would hold the pooled page forever.
MAX_EVALUATE_TIMEOUT_SECONDS = 5.0

#: Test-only injection point: when set, page reads bound at this ceiling
#: instead of the production constant. Never set in production wiring.
EVALUATE_CEILING_SECONDS_OVERRIDE: float | None = None


def render_phase_seconds(timeout_seconds: float) -> float:
    """The one render-phase budget, floored at 1s. Navigation has
    RETURNED before actions start, so the two grants are sequential, not
    additive -- both call sites read this declaration instead of re-deriving."""
    return max(RENDER_PHASE_FLOOR_SECONDS, float(timeout_seconds))


def _bounded_read_seconds() -> float:
    """The effective ceiling: the test override when set, else production."""
    override = EVALUATE_CEILING_SECONDS_OVERRIDE
    if override is None:
        return MAX_EVALUATE_TIMEOUT_SECONDS
    return float(override)


async def bounded_read(op: Awaitable[object]) -> object:
    """One page read under the evaluate ceiling. Raises TimeoutError when the
    ceiling trips; callers decide whether a wedged read degrades or fails."""
    return await asyncio.wait_for(op, timeout=_bounded_read_seconds())


async def bounded_evaluate(page: RenderPage, expression: str) -> object:
    """One page.evaluate under the evaluate ceiling; TimeoutError past it."""
    return await bounded_read(page.evaluate(expression))


async def bounded_content(page: RenderPage) -> str:
    """One page.content() under the evaluate ceiling; TimeoutError past it."""
    return cast(str, await bounded_read(page.content()))


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
