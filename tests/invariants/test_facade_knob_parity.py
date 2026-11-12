"""Facade knob-parity, core clauses: MCP advertisement and #808 execution reach. The field set is DERIVED
from the OpenAPI schema (wire truth off the served app), never hand-listed (#649 law); see
``_facade_tables`` for the derivation owner and the shared exclusion tables.
"""

from __future__ import annotations

from crawlutron_mcp.registry import TOOL_SPECS

from tests.invariants._facade_tables import (
    DICT_FIELD_ROUTES,
    MCP_ROUTE_EXCLUSIONS,
    REFUSAL_FIELD_NAMES,
    AcceptedKnob,
    _tool_spec_routes,
    accepted_knobs,
)
from tests.invariants._parity_rows import PRINCIPLED_ROWS


def _mcp_covered(knob: AcceptedKnob) -> bool:
    """MCP advertises the field, refuses it, or the route is principled-excluded."""
    if (knob.method, knob.path) in MCP_ROUTE_EXCLUSIONS:
        return True
    for spec in TOOL_SPECS:
        route = spec.rest_route
        if route is None or (route.method, route.path) != (knob.method, knob.path):
            continue
        if knob.field in spec.unsupported_fields:
            return True
        if f"{{{knob.field}}}" in route.path:
            return True
        return knob.field in spec.request_model.model_json_schema().get("properties", {})
    return False


def test_registry_has_one_spec_per_rest_route():
    """The registry invariant _mcp_covered's first-spec-wins scan depends on."""
    seen: set[tuple[str, str]] = set()
    for key in sorted(_tool_spec_routes()):
        assert key not in seen, f"a sibling already claims {key}"
        seen.add(key)


def test_mcp_advertises_or_refuses_every_knob():
    """Each accepted field is advertised by the route's tool or declared refused."""
    uncovered = [str(k) for k in accepted_knobs() if not _mcp_covered(k)]
    assert not uncovered, f"fields with no MCP advertisement or refusal: {uncovered}"


def _consumed_field_names() -> frozenset[str]:
    """Fields read by name in the app tree (attribute or literal access)."""
    import ast

    from tests._walk_readers import attribute_reads, string_literals
    from tests._walk_trees import app_trees

    reads: set[str] = set()
    for _path, tree in app_trees(exclude=frozenset()):
        for node in attribute_reads(tree):
            if isinstance(node, ast.Attribute):
                reads.add(node.attr)
        for literal in string_literals(tree):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                reads.add(literal.value)
    return frozenset(reads)


def test_accepted_knob_reaches_execution():
    """#808: every accepted knob is read in the app tree or refused at a door. Dict-typed bodies
    (policy/validators) prove reach at the route boundary: their model classes are extra=forbid at the
    validation chokepoint and the sub-knob vocabulary is that model's own field set (parent ruling 3).
    """
    consumed = _consumed_field_names()
    dropped = [
        str(knob)
        for knob in accepted_knobs()
        if knob.field not in REFUSAL_FIELD_NAMES
        and knob.field not in DICT_FIELD_ROUTES.get((knob.method, knob.path), frozenset())
        and knob.field not in consumed
    ]
    assert not dropped, f"accepted-but-unthreaded knobs: {dropped}"


def test_dict_typed_bodies_are_chokepoint_closed():
    """Each DICT_FIELD_ROUTES body field is a typed, extra=forbid model field."""
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.fetch_validators import FetchValidators

    for model in (CrawlPolicy, FetchValidators):
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} must forbid extras"
    crawl = next(spec for spec in TOOL_SPECS if spec.name == "crawl")
    assert "policy" in crawl.request_model.model_fields
    assert "validators" in crawl.request_model.model_fields


def test_derived_enumeration_covers_every_tool_model():
    """The enumeration is total: every TOOL_SPECS model field appears in it."""
    enumerated = {(k.method, k.path, k.field) for k in accepted_knobs()}
    for spec in TOOL_SPECS:
        route = spec.rest_route
        if route is None:
            continue
        for field in spec.request_model.model_fields:
            # A tool-model field naming the route's path parameter rides the
            # path itself (control_job carries job_id in its request model).
            if f"{{{field}}}" in route.path:
                continue
            assert (route.method, route.path, field) in enumerated, (
                f"TOOL_SPECS model {spec.request_model.__name__} field {field} "
                f"is not in the OpenAPI enumeration for {route.method} {route.path}"
            )


def test_refusal_vocabulary_answers_the_schema():
    """refusal_fields ⊆ unsupported_fields per spec (#670 drift law)."""
    for spec in TOOL_SPECS:
        refusal = getattr(spec.door, "refusal_fields", ())
        stray = set(refusal) - set(spec.unsupported_fields)
        assert not stray, f"tool {spec.name} refuses fields the schema drop-list omits: {stray}"


def test_keys_route_rides_the_principled_table():
    """The keys route's MCP absence comes from the shared principled table."""
    assert ("POST", "/v1/keys") in {(row.method, row.path) for row in PRINCIPLED_ROWS}
