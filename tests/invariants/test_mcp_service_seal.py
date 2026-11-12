"""The MCP layering seal: no module under ``crawlutron.mcp`` REACHES ``crawlutron.api``. Reach is transitive,
so the gate walks the runtime import graph and names the witness chain's endpoints; string-form importlib
is outside the walk.
"""

from scripts.module_graph import build_graph
from tests.invariants._seal_walk import offender_pairs as _walk_offenders

_SEALED_LAYER = "crawlutron.mcp."
_FORBIDDEN_LAYER = "crawlutron.api"


def _offender_pairs(modules: dict[str, set[str]]) -> set[str]:
    """Every (mcp module -> api module) reachability pair, as 'a -> b' strings."""
    return _walk_offenders(modules, _SEALED_LAYER, (_FORBIDDEN_LAYER,))


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
