"""The layer-exempt ratchet (#664): opting out is an explicit, gate-asserted act.

The pinned row list moves DOWN only; adding a row is RED until the owner
accepts the same-commit justification.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi.routing import APIRoute

from crawlutron.api.route_layer import LAYER_EXEMPT_RATCHET, ExemptRow
from crawlutron.api.router_registry import ROUTER_MODULES


def _guarded_keys() -> set[tuple[str, str]]:
    """The (method, path) pairs the layer guard protects, read off the routers."""
    from crawlutron.api.route_layer import LAYER_GUARDS

    keys: set[tuple[str, str]] = set()
    for router in ROUTER_MODULES:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            names = {dep.call.__name__ for dep in route.dependant.dependencies if dep.call is not None}
            if names & LAYER_GUARDS:
                for method in route.methods or ():
                    keys.add((method.lower(), route.path))
    return keys


def _served_routes() -> set[tuple[str, str]]:
    """Every (method, path) the real app serves, factory routers included."""
    from tests._app_factory import service_app

    app = service_app()
    assert app is not None
    keys: set[tuple[str, str]] = set()

    def walk(routes: Sequence[object]) -> None:
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                inner = getattr(route, "original_router", None)
                walk(getattr(inner, "routes", ()))
            elif isinstance(route, APIRoute):
                for method in route.methods or ():
                    keys.add((method.lower(), route.path))
            else:
                path, methods = getattr(route, "path", None), getattr(route, "methods", None)
                if path and methods:
                    keys.update((method.lower(), path) for method in methods)

    walk(app.router.routes)
    return keys


def test_exempt_rows_match_real_routes():
    """A stale exemption row (renamed or deleted route) fails the gate."""
    routes = _guarded_keys() | _served_routes()
    for row in LAYER_EXEMPT_RATCHET:
        path = row.path.split(":")[0]
        key = (row.method, path)
        assert key in routes, f"exempt row {key} no longer matches a served route"


def test_exempt_ratchet_never_increases():
    """The pinned literal only moves down, in the commit that earns it."""
    assert len(LAYER_EXEMPT_RATCHET) == 7, (
        "LAYER_EXEMPT_RATCHET is pinned at 7; adding a row demands the owner "
        "accept the row's justification in the same commit and lower/raise this "
        "literal deliberately -- an uncommented bump is the evasion this test exists for"
    )


def test_every_row_carries_a_justification():
    """An exemption without a stated reason is not an explicit act."""
    for row in LAYER_EXEMPT_RATCHET:
        assert isinstance(row, ExemptRow) and len(row.reason) > 20, f"row {row} lacks a real justification"
