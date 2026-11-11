"""The facade knob-parity derivation owner: ONE OpenAPI-derived enumeration
and the shared exclusion tables the three clause modules consume.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron.mcp.registry import TOOL_SPECS
from tests._app_factory import service_app
from tests.invariants._parity_rows import PRINCIPLED_ROWS

_BODY_METHODS = frozenset({"post", "put", "patch", "delete"})


@dataclass(frozen=True)
class AcceptedKnob:
    """One accepted request field on one route, derived from the OpenAPI schema."""

    method: str
    path: str
    field: str

    def __str__(self) -> str:
        return f"{self.method} {self.path}::{self.field}"


def accepted_knobs() -> tuple[AcceptedKnob, ...]:
    """Every body field, query parameter, and path parameter the served OpenAPI schema accepts."""
    openapi = service_app().openapi()
    knobs: list[AcceptedKnob] = []
    for path, item in openapi["paths"].items():
        shared = [p for p in item.get("parameters", []) if p.get("in") in ("query", "path")]
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            route = (method.upper(), path)
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            ref = body.get("schema", {}).get("$ref")
            if ref is not None:
                schema = openapi["components"]["schemas"][ref.rsplit("/", 1)[-1]]
                knobs.extend(AcceptedKnob(*route, field) for field in schema.get("properties", {}))
            knobs.extend(
                AcceptedKnob(*route, param["name"])
                for param in (*shared, *op.get("parameters", []))
                if param.get("in") in ("query", "path") and param.get("name")
            )
    return tuple(knobs)


#: /v1/keys has no MCP tool by principled exclusion: the operator trust plane,
#: shared with test_rest_mcp_parity's table by import (one plane, one table).
MCP_ROUTE_EXCLUSIONS: frozenset[tuple[str, str]] = frozenset(
    (row.method, row.path) for row in PRINCIPLED_ROWS
)

#: Refusal vocabulary across every tool: fields the MCP door refuses at
#: dispatch (unsupported) or guards (refusal_fields) -- #670 drift law.
REFUSAL_FIELD_NAMES: frozenset[str] = frozenset(
    field
    for spec in TOOL_SPECS
    for field in (*spec.unsupported_fields, *getattr(spec.door, "refusal_fields", ()))
)

#: SDK facade intentionally mirrors only the core doors (sdk_thin_by_design;
#: see #846 for the completeness follow-up).
SDK_THIN_ROWS: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/map"),
    ("POST", "/v1/render"),
)

#: Identity knobs the SDK accepts as METHOD parameters (delete_job(job_id) etc.);
#: #846 owns any richer shape, and /v1/keys has no SDK client at all.
SDK_KNOB_ROWS: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/v1/jobs"): frozenset({"limit", "offset"}),
    ("GET", "/v1/jobs/{job_id}"): frozenset({"job_id"}),
    ("POST", "/v1/jobs/{job_id}/control"): frozenset({"job_id"}),
    ("DELETE", "/v1/jobs/{job_id}"): frozenset({"job_id"}),
    ("GET", "/v1/watches/{watch_id}"): frozenset({"watch_id"}),
    ("DELETE", "/v1/watches/{watch_id}"): frozenset({"watch_id"}),
    ("GET", "/v1/crawl/{request_id}"): frozenset({"request_id"}),
    ("GET", "/v1/crawl/{request_id}/trace"): frozenset({"request_id"}),
    ("POST", "/v1/keys"): frozenset({"label", "max_concurrency", "max_pages"}),
    ("DELETE", "/v1/keys/{key_id}"): frozenset({"key_id"}),
}

#: The CLI forwards POST /v1/crawl only (audit-1: intentionally thin).
CLI_EXCLUDED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/admit"),
    ("POST", "/v1/jobs"),
    ("POST", "/v1/jobs/{job_id}/control"),
    ("POST", "/v1/keys"),
    ("POST", "/v1/map"),
    ("POST", "/v1/render"),
    ("POST", "/v1/search"),
    ("POST", "/v1/watches"),
    ("GET", "/v1/jobs"),
    ("GET", "/v1/jobs/{job_id}"),
    ("DELETE", "/v1/jobs/{job_id}"),
    ("GET", "/v1/crawl/{request_id}"),
    ("GET", "/v1/crawl/{request_id}/trace"),
    ("GET", "/v1/watches/{watch_id}"),
    ("DELETE", "/v1/watches/{watch_id}"),
    ("DELETE", "/v1/keys/{key_id}"),
)

#: CLI flags consumed client-side (polling, output shaping, auth transport);
#: they are not wire knobs and need no facade mirror.
CLI_CLIENT_ONLY_FLAGS: frozenset[str] = frozenset(
    {"key", "base_url", "trace", "pretty", "exit_on", "wait", "poll_interval"}
)

#: Dict-typed bodies whose reach is proven at the route boundary: the body is
#: validated at a chokepoint and consumed downstream (parent ruling 3).
DICT_FIELD_ROUTES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/v1/crawl"): frozenset({"policy", "validators"}),
    ("POST", "/v1/jobs"): frozenset({"policy"}),
    ("POST", "/v1/map"): frozenset({"policy"}),
}
