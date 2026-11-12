"""DDL stays in migrations; every MCP tool declares its validating model."""

from __future__ import annotations

import ast
import pathlib
import re
import typing

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    app_trees,
    call_nodes,
    calls_to_any,
    functions,
    nodes_of,
    offender_at,
)
from tests.invariants._shared import _relative_app_path
from tests.property_registry import Property
from tests.rules import Bites, Registry, Rule, Silent

_MIGRATION_DDL_PROPERTY = Property(
    name="migration-ddl",
    owner_module="crawlutron/storage/migrations/",
    owner_symbols=(),
    definition_site="crawlutron/storage/migrations/",
    fingerprints=("ddl",),
    reason="Schema changes must remain ordered and reversible in the migration package.",
)


def _ddl_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    if _relative_app_path(path).startswith("crawlutron/storage/migrations/"):
        return []
    found: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if re.match(
            r"\s*(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b",
            node.value,
            re.IGNORECASE,
        ):
            found.append(offender_at(path, node, node.value.splitlines()[0]))
    return found


DDL_IS_CONFINED_TO_THE_MIGRATIONS_PACKAGE = Rule(
    name="ddl-is-confined-to-the-migrations-package",
    find=lambda tree, path: _ddl_offenders(tree, path, _MIGRATION_DDL_PROPERTY),
    summary=(
        "CREATE/ALTER/DROP statements belong only in crawlutron/storage/migrations, where "
        "they are ordered and reversible; a DDL statement anywhere else is a "
        "schema change that ships without a migration"
    ),
    # The scan needs every module including the sanctioned wrappers, not the default SANCTIONED exclusion.
    scope=lambda *, exclude: app_trees(exclude=frozenset()),
    bite=lambda: Bites(
        "def bypass(conn):\n"
        "    conn.execute('CREATE TABLE x (id TEXT)')\n"
        "    conn.execute('ALTER TABLE x ADD COLUMN y TEXT')\n"
        "    conn.execute('DROP TABLE x')\n",
        "CREATE TABLE",
        count=3,
    ),
    clean=lambda: (
        # The migrations package is where DDL is allowed, so this shape is spared there and must fire at every
        # other module.
        Silent(
            "def apply(conn):\n    conn.execute('CREATE TABLE x (id TEXT)')\n",
            at=APP_ROOT / "storage" / "migrations" / "0001_crawl_jobs_initial.py",
        ),
    ),
)


# Two descriptions of one contract, neither derived from the other -- and the advertised one claimed
# additionalProperties: False for a request model that in fact ignores extras.
_MCP_TOOLS_MODULE = "crawlutron/mcp/registry.py"


def _mcp_unvalidated_tool_offenders(tree, path, *, tools_module):
    if path.name != tools_module:
        return
    for call in calls_to_any(tree, {"ToolSpec"}):
        named = {kw.arg for kw in call.keywords if kw.arg}
        if "request_model" not in named:
            yield offender_at(
                path,
                call,
                "ToolSpec registers a tool without a request_model; the inputSchema and the call's argument "
                "validation are both derived from it, so a tool without one has no validated contract",
            )
    # The dispatcher must be the one validating, not the handlers.
    for node in functions(tree):
        if not node.name.startswith("_handle_"):
            continue
        for inner in call_nodes(node):
            if isinstance(inner.func, ast.Attribute) and inner.func.attr == "model_json_schema":
                yield offender_at(
                    path,
                    inner,
                    f"{node.name}() still derives a schema itself; the "
                    f"schema belongs to ToolSpec.input_schema, derived from the request model",
                )


#: The module's own basename, compared by the detector to each swept file's ``path.name``. The old scan passed
#: the full repo-relative path, which never matched a basename -- sweeping nothing and reporting
_MCP_TOOLS_BASENAME = "registry.py"


EVERY_MCP_TOOL_DECLARES_THE_MODEL_THAT_VALIDATES_ITS_ARGUMENTS = Rule(
    name="every-mcp-tool-declares-the-model-that-validates-its-arguments",
    find=lambda tree, path: _mcp_unvalidated_tool_offenders(tree, path, tools_module=_MCP_TOOLS_BASENAME),
    summary=(
        "The inputSchema and the call's argument validation are both derived from "
        "the pydantic request model, so a tool without one has no validated "
        "contract and a handler that derives its own schema bypasses the dispatcher"
    ),
    proof_path=REPO_ROOT / _MCP_TOOLS_MODULE,
    bite=lambda: (
        Bites(
            "def register():\n    return ToolSpec(\n        name='x',\n        description='d',\n"
            "        output_schema=None,\n        handler=h,\n    )\n",
            "no validated contract",
        ),
        # A handler that builds its own schema is the other half of the bypass.
        Bites(
            "async def _handle_x(payload, settings, shutdown_event):\n"
            "    schema = CrawlRequest.model_json_schema()\n"
            "    return schema\n",
            "still derives a schema itself",
        ),
    ),
)


def test_advertised_mcp_schemas_come_from_the_request_models():
    """The live registry satisfies the invariant the guard asserts.

    The bite proves the detector; this proves the property on the real
    TOOL_SPECS: no tool ships a schema its request model does not describe.
    """
    from crawlutron.mcp.registry import TOOL_SPECS

    for spec in TOOL_SPECS:
        assert spec.request_model is not None, spec.name
        assert spec.input_schema["properties"] == {
            k: v
            for k, v in spec.request_model.model_json_schema().get("properties", {}).items()
            if k not in spec.unsupported_fields
        }, f"{spec.name} advertises a schema its request model does not describe"


RULES = Registry(
    (
        DDL_IS_CONFINED_TO_THE_MIGRATIONS_PACKAGE,
        EVERY_MCP_TOOL_DECLARES_THE_MODEL_THAT_VALIDATES_ITS_ARGUMENTS,
    )
)


def test_discovery_response_fields_match_the_payload_declaration():
    """The validated twin's field set cannot drift from ``DiscoveryPayload``.

    The payload is the declaration of record; the response model redeclares its
    fields for pydantic, and this invariant proves the redeclaration agrees.
    """
    from crawlutron.models.discovery_response import DiscoveryPayload, DiscoveryResponse

    payload_hints = typing.get_type_hints(DiscoveryPayload)
    response_hints = typing.get_type_hints(DiscoveryResponse)
    assert set(response_hints) == set(payload_hints)
    for name, annotation in payload_hints.items():
        assert response_hints[name] == annotation, name


def test_no_rule_violations():
    """DDL lives in migrations; every MCP tool declares its validating model."""
    RULES.check_all_clean()
