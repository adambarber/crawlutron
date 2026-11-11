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

The allowlist below declares the DIRECT residual edges; the gate derives their
transitive closure, so a residual edge whose fallout reaches other api modules
names every fallout pair too — the reported offender set is exactly the
closure of the declared rows, both directions checked. Each row cites the
commit body that landed it; the set is a RATCHET DOWN at later commits: a row
dies when its edge dies, and a stale row (edge gone) is itself a failure.
Weakening the seal — an uncited row, or shrinking the scanned set — is a
defect, not a fix.
"""

from __future__ import annotations

from scripts.module_graph import build_graph

__all__ = ["RESIDUAL_EDGES"]

_SEALED_LAYER = "crawlutron.mcp."
_FORBIDDEN_LAYER = "crawlutron.api"

#: Named residuals: (mcp module, DIRECT api module it imports) with the reason's
#: commit; the gate closes each over the transitive graph. Sealed EMPTY by #861.
RESIDUAL_EDGES: dict[tuple[str, str], str] = {}


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


def _allowed_pairs(modules: dict[str, set[str]]) -> set[str]:
    """The closure of the declared residual edges over the import graph.

    Fallout flows BOTH ways through mcp-internal imports (registry -> handlers
    re-exports the residual reach), so a module that reaches a residual's api
    target inherits the same allowance for that target's downstream only."""
    allowed: set[str] = set()
    for _mcp_module, api_module in RESIDUAL_EDGES:
        fallout = {r for r in _reachable(modules, api_module) if r.startswith(_FORBIDDEN_LAYER)}
        for name in modules:
            if not name.startswith(_SEALED_LAYER):
                continue
            if api_module in _reachable(modules, name):
                allowed.update(f"{name} -> {reached}" for reached in fallout)
    return allowed


def test_no_mcp_module_reaches_api() -> None:
    """The seal: DFS over the runtime import graph; offenders minus the closure."""
    modules, _importers = build_graph()
    offenders = _offender_pairs(modules)
    unallowed = offenders - _allowed_pairs(modules)
    assert not unallowed, (
        "mcp reaches api outside the named residuals "
        "(each needs a commit-body citation or an ownership move):\n" + "\n".join(sorted(unallowed))
    )


def test_no_stale_residual_rows() -> None:
    """A row whose direct edge died is deleted in the same commit, not left."""
    modules, _importers = build_graph()
    for mcp_module, api_module in RESIDUAL_EDGES:
        assert api_module in modules.get(mcp_module, ()), (
            f"stale residual row (the direct edge died): {mcp_module} -> {api_module}"
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
        "crawlutron.mcp.handlers": {"crawlutron.api.render_endpoint", "crawlutron.api.route_layer"},
        "crawlutron.api.render_endpoint": {"crawlutron.api.render"},
        "crawlutron.api.render": set(),
        "crawlutron.api.route_layer": set(),
    }
    offenders = _offender_pairs(planted)
    allowed = _allowed_pairs(planted)
    unallowed = offenders - allowed
    assert unallowed == offenders, unallowed
    assert "crawlutron.mcp.handlers -> crawlutron.api.route_layer" in unallowed
    assert "crawlutron.mcp.handlers -> crawlutron.api.render_endpoint" in unallowed
    assert "crawlutron.mcp.handlers -> crawlutron.api.render" in unallowed


def test_every_residual_row_cites_a_commit_body() -> None:
    """An allowlist row without its citation is an unreviewed exemption."""
    for (mcp_module, api_module), reason in RESIDUAL_EDGES.items():
        assert mcp_module.startswith(_SEALED_LAYER), f"non-mcp row: {mcp_module}"
        assert api_module.startswith(_FORBIDDEN_LAYER), f"non-api row: {api_module}"
        assert "#849" in reason and "C4-owed" in reason, f"uncited row: {mcp_module} -> {api_module}"


def test_residual_rows_name_live_modules() -> None:
    """A row naming a module that no longer exists is a stale row, not history."""
    modules, _importers = build_graph()
    for mcp_module, api_module in RESIDUAL_EDGES:
        assert mcp_module in modules, f"residual row names a dead module: {mcp_module}"
        assert api_module in modules, f"residual row names a dead module: {api_module}"
