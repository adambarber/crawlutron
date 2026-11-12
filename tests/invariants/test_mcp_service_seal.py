"""The MCP layering seal: no module under ``crawlutron_mcp`` REACHES ``crawlutron_rest``
(reach is transitive; string-form importlib is out). After the api/main/cli move
(#902) the forbidden layer is the whole rest wheel.
"""

from scripts.module_graph import build_graph
from tests.invariants._seal_walk import assert_sealed_layer_present as _assert_present
from tests.invariants._seal_walk import offender_pairs as _walk_offenders

_SEALED_LAYER = "crawlutron_mcp."
_FORBIDDEN_LAYER = "crawlutron_rest"


def _offender_pairs(modules: dict[str, set[str]]) -> set[str]:
    """Every (mcp module -> api module) reachability pair, as 'a -> b' strings."""
    return _walk_offenders(modules, _SEALED_LAYER, (_FORBIDDEN_LAYER,))


def test_no_mcp_module_reaches_api() -> None:
    """The seal: DFS over the runtime import graph; the sealed layer must be present
    first, or a vanished root would green vacuously over an empty set."""
    modules, _importers = build_graph()
    _assert_present(modules, _SEALED_LAYER)
    offenders = _offender_pairs(modules)
    assert not offenders, (
        "mcp reaches the rest wheel outside the named residuals "
        "(each needs a commit-body citation or an ownership move):\n" + "\n".join(sorted(offenders))
    )


def test_seal_names_both_endpoints_of_a_two_hop_chain() -> None:
    """The gate is not a one-level scan: a planted mcp -> fresh -> rest chain reds
    naming BOTH files. Synthetic graph, same walk the live gate runs."""
    planted = {
        "crawlutron_mcp.planted_a": {"crawlutron_mcp.planted_b"},
        "crawlutron_mcp.planted_b": {"crawlutron_rest.api.planted_c"},
        "crawlutron_rest.api.planted_c": set(),
    }
    offenders = _offender_pairs(planted)
    assert set(offenders) == {
        "crawlutron_mcp.planted_a -> crawlutron_rest.api.planted_c",
        "crawlutron_mcp.planted_b -> crawlutron_rest.api.planted_c",
    }, offenders


def test_seal_refuses_every_mcp_to_rest_edge_with_empty_residuals() -> None:
    """With the allowlist sealed empty, any planted mcp->rest edge is an offender:
    the residual closure is empty so nothing is allowed."""
    planted = {
        "crawlutron_mcp.handlers": {"crawlutron_rest.api.route_layer"},
        "crawlutron_rest.api.route_layer": set(),
    }
    offenders = _offender_pairs(planted)
    allowed: set[str] = set()
    unallowed = offenders - allowed
    assert unallowed == offenders, unallowed
    assert "crawlutron_mcp.handlers -> crawlutron_rest.api.route_layer" in unallowed
