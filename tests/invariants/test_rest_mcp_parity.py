"""REST/MCP parity: every non-excluded REST route has >=1 MCP tool covering it.

Walk sources are the seven module-level routers only; factory-built discovery
and health routers are not walk sources and the app is never constructed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.routing import APIRoute

from crawlutron.api.router_registry import ROUTER_MODULES
from crawlutron.mcp.tools import TOOL_SPECS


@dataclass(frozen=True)
class ParityRow:
    """One REST route the parity gate knowingly leaves without MCP coverage."""

    method: str
    path: str
    reason: str


#: ACCIDENT rows are temporary gaps: each is deleted in the same commit as the
#: tool that lands. The ratchet constant below must move DOWN only.
ACCIDENT_ROWS: tuple[ParityRow, ...] = (
    ParityRow("GET", "/v1/crawl/{request_id}", "read_crawl_result"),
    ParityRow("GET", "/v1/crawl/{request_id}/trace", "read_crawl_trace"),
    ParityRow("POST", "/v1/jobs", "create_job"),
    ParityRow("GET", "/v1/jobs", "list_jobs"),
    ParityRow("GET", "/v1/jobs/{job_id}", "get_job"),
    ParityRow("POST", "/v1/jobs/{job_id}/control", "control_job"),
    ParityRow("DELETE", "/v1/jobs/{job_id}", "delete_job"),
    ParityRow("POST", "/v1/watches", "create_watch"),
    ParityRow("GET", "/v1/watches", "list_watches"),
    ParityRow("GET", "/v1/watches/{watch_id}", "get_watch"),
    ParityRow("DELETE", "/v1/watches/{watch_id}", "delete_watch"),
)

#: Pinned literal: rows and this count move DOWN together, same commit.
ACCIDENT_ROW_COUNT: int = 11

#: PRINCIPLED rows are permanent, deliberate absences with a stated reason.
PRINCIPLED_ROWS: tuple[ParityRow, ...] = (
    ParityRow("GET", "/v1/policy-suggestions", "policy-suggestions pending stretch scope"),
    ParityRow("POST", "/v1/keys", "keys are the operator trust plane"),
    ParityRow("GET", "/v1/keys", "keys are the operator trust plane"),
    ParityRow("DELETE", "/v1/keys/{key_id}", "keys are the operator trust plane"),
    ParityRow("GET", "/metrics", "operator metrics endpoint, not a crawl capability"),
)

EXCLUDED_ROUTES = {(row.method, row.path) for row in (*ACCIDENT_ROWS, *PRINCIPLED_ROWS)}


def _registered_routes() -> dict[tuple[str, str], str]:
    """Every (method, path) the module-level router registry registers."""
    routes: dict[tuple[str, str], str] = {}
    for router in ROUTER_MODULES:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            # APIRoute.methods is typed set[str] | None; an APIRoute with no
            # methods carries no parity obligation.
            for method in route.methods or ():
                routes.setdefault((method, route.path), type(router).__module__)
    return routes


def test_every_exclusion_row_matches_a_registered_route():
    """Stale-exclusion guard: a renamed or deleted route fails the gate."""
    routes = _registered_routes()
    for row in (*ACCIDENT_ROWS, *PRINCIPLED_ROWS):
        key = (row.method, row.path)
        assert key in routes, f"exclusion row {key} no longer matches a registered route"


def test_every_non_excluded_route_has_tool_coverage():
    """Each registered route not in the exclusion table is covered by >=1 tool."""
    routes = _registered_routes()
    covered = {
        (spec.rest_route.method, spec.rest_route.path) for spec in TOOL_SPECS if spec.rest_route is not None
    }
    uncovered = [key for key in routes if key not in EXCLUDED_ROUTES and key not in covered]
    assert not uncovered, f"routes without MCP tool coverage: {sorted(uncovered)}"


def test_tool_routes_are_unique_and_registered():
    """A tool covers at most one route, and that route must actually exist."""
    routes = _registered_routes()
    seen: dict[tuple[str, str], str] = {}
    for spec in TOOL_SPECS:
        if spec.rest_route is None:
            continue
        key = (spec.rest_route.method, spec.rest_route.path)
        assert key in routes, f"tool {spec.name} points at unregistered route {key}"
        assert key not in seen, f"tools {seen[key]} and {spec.name} both claim {key}"
        seen[key] = spec.name


def test_accident_ratchet_never_increases():
    """The pinned literal only moves down, in the commit that earns it."""
    assert len(ACCIDENT_ROWS) == ACCIDENT_ROW_COUNT, (
        f"ACCIDENT_ROW_COUNT is pinned at {ACCIDENT_ROW_COUNT}; adding a row "
        "requires landing its tool in the same commit and lowering the literal"
    )
