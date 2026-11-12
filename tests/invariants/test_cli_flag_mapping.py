"""CLI flag-mapping pin: every wire-bound CLI flag names a real OpenAPI field. The flag->field table is
DERIVED from ``crawlutron_rest.cli.__main__``'s ``_build_payload`` at test time via AST and validated against
the OpenAPI-derived schema components, so flag->renamed-field drift reds at collection.
"""

from __future__ import annotations

import ast
import pathlib

from tests._app_factory import service_app
from tests.invariants._facade_tables import CLI_CLIENT_ONLY_FLAGS, accepted_knobs

_CLI_MAIN = (
    pathlib.Path(__file__).resolve().parents[2]
    / "packages"
    / "rest"
    / "crawlutron_rest"
    / "cli"
    / "__main__.py"
)
_ROUTE = ("POST", "/v1/crawl")
#: Container variable -> wire schema that owns the keys written into it.
_CONTAINER_SCHEMAS = {"policy": "CrawlPolicy", "limits": "CrawlPolicyLimits"}


def _string_keys(nodes: list[ast.expr | None]) -> set[str]:
    return {node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _payload_keys(fn: ast.FunctionDef) -> dict[str, set[str]]:
    """Wire keys _build_payload may emit, grouped by the container they target."""
    grouped: dict[str, set[str]] = {"payload": set()}
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            grouped["payload"] |= _string_keys(node.keys)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            container = node.value.id
            grouped.setdefault(container, set())
            grouped[container] |= _string_keys([node.slice])
    grouped["payload"] -= {"policy", "limits"}
    return grouped


def _build_payload() -> ast.FunctionDef:
    tree = ast.parse(_CLI_MAIN.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_payload":
            return node
    raise AssertionError("_build_payload not found in cli/__main__.py")


def _schema_properties(name: str) -> frozenset[str]:
    schema: dict[str, object] = service_app().openapi()["components"]["schemas"][name]
    properties = schema.get("properties", {})
    assert isinstance(properties, dict)
    return frozenset(str(key) for key in properties)


def test_cli_payload_keys_name_served_crawl_fields():
    """Top-level payload keys are accepted POST /v1/crawl knobs."""
    served = {k.field for k in accepted_knobs() if (k.method, k.path) == _ROUTE}
    keys = _payload_keys(_build_payload())
    assert keys["payload"], "derivation found no payload keys (AST probe broke)"
    unknown = sorted(keys["payload"] - served)
    assert not unknown, f"CLI payload keys naming no POST /v1/crawl field: {unknown}"


def test_cli_nested_policy_keys_name_wire_schema_fields():
    """Keys written into policy/limits name fields of their wire schema."""
    grouped = _payload_keys(_build_payload())
    drifts: list[str] = []
    for container, schema_name in _CONTAINER_SCHEMAS.items():
        served = _schema_properties(schema_name)
        drifts.extend(
            f"{container}.{key} not in {schema_name}"
            for key in sorted(grouped.get(container, set()) - served)
        )
    assert not drifts, f"CLI nested keys naming no wire schema field: {drifts}"


def test_client_only_flags_do_not_collide_with_wire_fields():
    """Client-only flags stay outside the wire-key derivation (no dual lives)."""
    all_keys = set().union(*_payload_keys(_build_payload()).values())
    overlap = sorted(CLI_CLIENT_ONLY_FLAGS & all_keys)
    assert not overlap, f"flags claimed client-only also map to wire fields: {overlap}"
