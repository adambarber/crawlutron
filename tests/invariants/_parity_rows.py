"""The parity exclusion tables' single home.

``test_rest_mcp_parity``, ``_facade_tables``, and ``test_facade_knob_parity``
import from here (three importers, one table) -- a test-to-test import of a
shared production-adjacent constant is the #670 one-owner law applied to the
parity plane.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParityRow:
    """One REST route the parity gate knowingly leaves without MCP coverage."""

    method: str
    path: str
    reason: str


#: ACCIDENT rows are temporary gaps: each is deleted in the same commit as the
#: tool that lands; the ratchet moves DOWN only. #648 sealed it shut at zero.
ACCIDENT_ROWS: tuple[ParityRow, ...] = ()

#: Pinned literal: rows and this count move DOWN together, same commit.
#: #647: create_job landed; 5 -> 4. #648: the watch tools landed; 4 -> 0 (sealed).
ACCIDENT_ROW_COUNT: int = 0

#: PRINCIPLED rows are permanent, deliberate absences with a stated reason.
PRINCIPLED_ROWS: tuple[ParityRow, ...] = (
    ParityRow("POST", "/v1/keys", "keys are the operator trust plane"),
    ParityRow("GET", "/v1/keys", "keys are the operator trust plane"),
    ParityRow("DELETE", "/v1/keys/{key_id}", "keys are the operator trust plane"),
    ParityRow("GET", "/metrics", "operator metrics endpoint, not a crawl capability"),
)

EXCLUDED_ROUTES = {(row.method, row.path) for row in (*ACCIDENT_ROWS, *PRINCIPLED_ROWS)}

__all__ = [
    "ACCIDENT_ROW_COUNT",
    "ACCIDENT_ROWS",
    "EXCLUDED_ROUTES",
    "PRINCIPLED_ROWS",
    "ParityRow",
]
