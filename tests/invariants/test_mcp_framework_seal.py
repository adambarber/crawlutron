"""The MCP framework-freeness seal: no ``crawlutron_mcp`` module reaches fastapi or starlette.
Reach is transitive via the shared walk, naming the violating (mcp module -> forbidden) pair;
the alias-proof import resolution lives in the module graph, the reach chokepoint.
"""

from __future__ import annotations

import ast

from scripts import module_graph
from scripts.module_graph import build_graph
from tests.invariants._seal_walk import offender_pairs as _walk_offenders

_SEALED_LAYER = "crawlutron_mcp."
_FORBIDDEN_LAYERS = ("fastapi", "starlette")


def test_no_mcp_module_imports_fastapi_or_starlette() -> None:
    """The seal: DFS over the runtime import graph; any offender is a finding."""
    modules, _importers = build_graph()
    offenders = _walk_offenders(modules, _SEALED_LAYER, _FORBIDDEN_LAYERS)
    assert not offenders, (
        "crawlutron_mcp reaches fastapi/starlette; the MCP protocol layer is "
        "framework-free so it can embed in any ASGI server:\n" + "\n".join(sorted(offenders))
    )


def test_seal_names_the_bare_package_root_source() -> None:
    """The package root (crawlutron_mcp/__init__.py) is a sealed source itself."""
    planted = {
        "crawlutron_mcp": {"fastapi"},
        "fastapi": set(),
    }
    offenders = _walk_offenders(planted, _SEALED_LAYER, _FORBIDDEN_LAYERS)
    assert "crawlutron_mcp -> fastapi" in offenders, offenders


def test_seal_catches_aliased_and_nested_import_shapes() -> None:
    """Aliased/spelled imports reach the forbidden root via the production resolver.
    The test drives module_graph._imports directly because no public door serves synthetic
    source strings; its output is what build_graph feeds the walk (module_graph.py:123)."""
    for form in (
        "from fastapi import HTTPException as HX",
        "import fastapi.responses",
        "from starlette.middleware import base",
        "import starlette as st",
    ):
        reached = module_graph._imports(ast.parse(form))
        assert reached & set(_FORBIDDEN_LAYERS), f"the module graph missed the planted form: {form}"
