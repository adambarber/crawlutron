"""The service-layer seal: no module under ``crawlutron.service`` REACHES fastapi.

DFS over ``scripts.module_graph`` edges (TYPE_CHECKING excluded) from every service
module; fastapi/starlette reach names the offender. Wire lives in api/health only. As
with the mcp seal, ``importlib.import_module`` string forms are outside this walk.
"""

from scripts.module_graph import build_graph
from tests.invariants._seal_walk import offender_pairs as _walk_offenders

_SEALED_LAYER = "crawlutron.service."
_FORBIDDEN_LAYERS = ("fastapi", "starlette")


def _offender_chains(modules: dict[str, set[str]]) -> set[str]:
    """Every (service module -> forbidden module) reachability pair."""
    return _walk_offenders(modules, _SEALED_LAYER, _FORBIDDEN_LAYERS)


def test_no_service_module_reaches_fastapi() -> None:
    """The seal: DFS over the runtime import graph; any offender is a finding."""
    modules, _importers = build_graph()
    offenders = _offender_chains(modules)
    assert not offenders, (
        "crawlutron.service reaches fastapi/starlette; the service core is "
        "framework-free (move the state/logic, keep the wire in api/health):\n" + "\n".join(sorted(offenders))
    )


def test_seal_walks_transitive_chains_not_one_level() -> None:
    """The gate is not a one-level scan: a planted service -> middle -> fastapi
    chain reds naming BOTH hops. Synthetic graph, same walk the live gate runs."""
    planted = {
        "crawlutron.service.planted_a": {"crawlutron.service.planted_b"},
        "crawlutron.service.planted_b": {"crawlutron.health.planted_router"},
        "crawlutron.health.planted_router": {"fastapi.routing"},
        "fastapi.routing": set(),
    }
    offenders = _offender_chains(planted)
    assert offenders == {
        "crawlutron.service.planted_a -> fastapi.routing",
        "crawlutron.service.planted_b -> fastapi.routing",
    }, offenders


def test_seal_names_direct_planted_edge() -> None:
    """A direct planted import in a service module is an offender naming both
    module endpoints (the file+module the fix round needs)."""
    planted = {
        "crawlutron.service.planted_direct": {"fastapi.requests"},
        "fastapi.requests": set(),
    }
    offenders = _offender_chains(planted)
    assert offenders == {"crawlutron.service.planted_direct -> fastapi.requests"}, offenders


def test_seal_refuses_starlette_too() -> None:
    """starlette is fastapi's transport; a service -> starlette reach is the same
    framework taint and reds identically."""
    planted = {
        "crawlutron.service.planted_starlette": {"starlette.middleware"},
        "starlette.middleware": set(),
    }
    offenders = _offender_chains(planted)
    assert offenders == {"crawlutron.service.planted_starlette -> starlette.middleware"}, offenders
