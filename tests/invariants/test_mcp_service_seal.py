"""The MCP layering seal: no module under ``crawlutron.mcp`` REACHES ``crawlutron.api``.

Reach is transitive: a one-level import scan misses ``mcp -> service -> api``, so
this gate walks the runtime import graph (``scripts.module_graph``, TYPE_CHECKING
edges excluded) from every ``crawlutron.mcp`` module and refuses any module that
reaches ``crawlutron.api``, naming the witness chain's both endpoints. The boundary
is import-STATEMENT reach (``ast.walk`` catches nested/lazy imports); a runtime
``importlib.import_module`` string form is a different mechanism this gate does not
scan — nothing under ``crawlutron/mcp/`` uses it. The MCP protocol layer imports
service (and models/vocabulary/storage), never api, even transitively; api owns the
HTTP-wire door (``api.mcp_router``).

The residual table is sealed empty: every mcp->api edge that ever existed was
drained by #849/C4 and #861, and the offender count is zero, so no exemption
surface survives the sealed gate. `test_no_mcp_module_reaches_api()` is the
unconditional assertion — it reds on any live mcp->api reach, naming both module
endpoints. Weakening the seal is a defect, not a fix.
"""

from scripts.module_graph import build_graph

_SEALED_LAYER = "crawlutron.mcp."
_FORBIDDEN_LAYER = "crawlutron.api"


def _reachable(modules: dict[str, set[str]], start: str) -> set[str]:
    """Every module transitively reachable from ``start`` (inclusive of start)."""
    seen: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(modules.get(current, ()))
    return seen


def _offender_pairs(modules: dict[str, set[str]]) -> set[str]:
    """Every (mcp module -> api module) reachability pair, as 'a -> b' strings."""
    offenders: set[str] = set()
    for name in modules:
        if not name.startswith(_SEALED_LAYER):
            continue
        for reached in _reachable(modules, name):
            if reached.startswith(_FORBIDDEN_LAYER):
                offenders.add(f"{name} -> {reached}")
    return offenders


def test_no_mcp_module_reaches_api() -> None:
    """The seal: DFS over the runtime import graph; any offender is a finding."""
    modules, _importers = build_graph()
    offenders = _offender_pairs(modules)
    assert not offenders, (
        "mcp reaches api outside the named residuals "
        "(each needs a commit-body citation or an ownership move):\n" + "\n".join(sorted(offenders))
    )


def test_seal_names_both_endpoints_of_a_two_hop_chain() -> None:
    """The gate is not a one-level scan: a planted mcp -> fresh -> api chain reds
    naming BOTH files. Synthetic graph, same walk the live gate runs."""
    planted = {
        "crawlutron.mcp.planted_a": {"crawlutron.mcp.planted_b"},
        "crawlutron.mcp.planted_b": {"crawlutron.api.planted_c"},
        "crawlutron.api.planted_c": set(),
    }
    offenders = _offender_pairs(planted)
    assert set(offenders) == {
        "crawlutron.mcp.planted_a -> crawlutron.api.planted_c",
        "crawlutron.mcp.planted_b -> crawlutron.api.planted_c",
    }, offenders


def test_seal_refuses_every_mcp_to_api_edge_with_empty_residuals() -> None:
    """With the allowlist sealed empty, any planted mcp->api edge is an offender:
    the residual closure is empty so nothing is allowed."""
    planted = {
        "crawlutron.mcp.handlers": {"crawlutron.api.route_layer"},
        "crawlutron.api.route_layer": set(),
    }
    offenders = _offender_pairs(planted)
    allowed: set[str] = set()
    unallowed = offenders - allowed
    assert unallowed == offenders, unallowed
    assert "crawlutron.mcp.handlers -> crawlutron.api.route_layer" in unallowed
