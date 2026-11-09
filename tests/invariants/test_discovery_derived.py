"""The discovery payload is derived truth, never a hand-maintained copy.

The well-known recommendation list and the agent-hints MCP guidance must be
computed from the live surface (OpenAPI + TOOL_SPECS), so a route or tool that
lands without updating discovery REDS here -- the reverse of the historical
staleness tests, which only caught advertised-but-dead entries.
"""

from __future__ import annotations

from crawlutron.config.settings import CrawlerSettings
from crawlutron.discovery.recommended_surface import OPERATOR_EXCLUSIONS
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
    """Derived from the production trust plane plus the self-door (test-only)."""
    return {
        *OPERATOR_EXCLUSIONS,
        "GET /.well-known/crawler-service",
    }


def test_discovery_exclusions_are_derived_from_the_production_trust_plane():
    """The test-side exclusion set is production's OPERATOR_EXCLUSIONS plus only
    the self-door: a hand-edited addition silently drops live routes from the
    coverage check, so the set is pinned by equality."""
    assert set(_discovery_exclusions()) == {
        *OPERATOR_EXCLUSIONS,
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


def test_no_hand_listed_endpoints_in_the_discovery_package():
    """The no-hand-list scan covers every discovery module EXCEPT
    recommended_surface.py -- the declared derivation input, where the doors
    and the operator trust plane legitimately live. A hand list planted in
    well_known.py, recommended.py, or agent_hints.py REDS here."""
    import ast
    import pathlib

    import crawlutron.discovery as discovery_pkg

    exempt = {"recommended_surface.py"}
    offenders: list[str] = []
    for path in sorted(pathlib.Path(discovery_pkg.__file__).parent.glob("*.py")):
        if path.name in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            parts = node.value.split()
            if len(parts) == 2 and parts[0] in {"GET", "POST", "DELETE", "PUT"}:
                offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
    assert not offenders, (
        "hand-listed endpoints in the discovery package; derive from the "
        f"served surface or recommended_surface: {offenders}"
    )


def test_crawl_refusal_fields_never_exceed_the_schema_drop_list():
    """ToolSpec defense-in-depth: no door may refuse a field its schema still
    advertises -- unsupported_fields owns the vocabulary and refusal_fields
    must stay within it (the #670 drift assert)."""
    from crawlutron.mcp.registry import TOOL_SPECS

    for spec in TOOL_SPECS:
        refusal = spec.door.refusal_fields if spec.door else ()
        unrefused = set(refusal) - set(spec.unsupported_fields)
        assert not unrefused, (
            f"tool {spec.name!r} refuses fields the schema still advertises "
            f"(absent from unsupported_fields): {sorted(unrefused)}"
        )


def test_mcp_guidance_names_every_registered_tool():
    """The hints' MCP guidance is derived from TOOL_SPECS: a new tool REDS until
    the guidance carries it -- hand-written tool lists cannot drift."""
    from crawlutron.discovery.agent_hints import agent_hints_payload

    guidance = " ".join(agent_hints_payload()["guidance"])
    unadvertised = [spec.name for spec in TOOL_SPECS if spec.name not in guidance]
    assert not unadvertised, f"MCP tools absent from agent-hints guidance: {unadvertised}"
