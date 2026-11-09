"""The discovery payload is derived truth, never a hand-maintained copy.

The well-known recommendation list and the agent-hints MCP guidance must be
computed from the live surface (OpenAPI + TOOL_SPECS), so a route or tool that
lands without updating discovery REDS here -- the reverse of the historical
staleness tests, which only caught advertised-but-dead entries.
"""

from __future__ import annotations

from crawlutron.config.settings import CrawlerSettings
from crawlutron.discovery.well_known import discovery_payload
from crawlutron.mcp.registry import TOOL_SPECS


def _live_surface() -> set[str]:
    from tests._app_factory import service_app

    service = service_app()
    return {
        f"{method.upper()} {route}"
        for route, operations in service.openapi()["paths"].items()
        for method in operations
    }


def _discovery_exclusions() -> set[str]:
    """Surfaces deliberately outside the recommendation list: the operator trust
    plane (keys are minted by the operator, not self-served) and metrics."""
    return {
        "GET /metrics",
        "POST /v1/keys",
        "GET /v1/keys",
        "DELETE /v1/keys/{key_id}",
        "GET /.well-known/crawler-service",
    }


def test_recommended_endpoints_cover_the_whole_served_surface():
    """The converse drift: a live route absent from the list is a door no agent finds."""
    recommended = set(discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"])
    live = _live_surface()
    missing = live - recommended - _discovery_exclusions()
    assert not missing, f"live routes absent from recommended_endpoints: {sorted(missing)}"
    extra = recommended - live
    assert not extra, f"recommended_endpoints name routes the app does not serve: {sorted(extra)}"


def test_recommended_endpoints_are_derived_not_hand_listed():
    """The payload builder must COMPUTE the list from the served surface, not hold
    a literal: the hand-curated copy is the drift this ticket seals."""
    import ast
    import inspect

    from crawlutron.discovery import well_known

    tree = ast.parse(inspect.getsource(well_known))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    endpoint_literals = {
        s for s in literals if s.split(" ", 1)[0] in {"GET", "POST", "DELETE"} and len(s.split(" ")) == 2
    }
    assert not endpoint_literals, (
        "recommended_endpoints are hand-listed in well_known.py; derive them from "
        f"the served surface: {sorted(endpoint_literals)}"
    )


def test_mcp_guidance_names_every_registered_tool():
    """The hints' MCP guidance is derived from TOOL_SPECS: a new tool REDS until
    the guidance carries it -- hand-written tool lists cannot drift."""
    from crawlutron.discovery.agent_hints import agent_hints_payload

    guidance = " ".join(agent_hints_payload()["guidance"])
    unadvertised = [spec.name for spec in TOOL_SPECS if spec.name not in guidance]
    assert not unadvertised, f"MCP tools absent from agent-hints guidance: {unadvertised}"
