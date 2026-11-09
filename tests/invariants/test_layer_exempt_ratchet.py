"""The layer-exempt ratchet (#664): opting out is an explicit, gate-asserted act.

The pinned row list moves DOWN only; adding a row is RED until the owner
accepts the same-commit justification.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastapi.routing import APIRoute

from crawlutron.api.route_layer import ANONYMOUS_METERED, LAYER_EXEMPT_RATCHET, ExemptRow
from crawlutron.api.router_registry import ROUTER_MODULES
from tests.invariants.test_route_layer import _offender_module


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
    keys: set[tuple[str, str]] = set()

    def collect(route: object) -> None:
        methods = getattr(route, "methods", None) or ()
        path = getattr(route, "path", None)
        if path and methods:
            keys.update((method.lower(), path) for method in methods)

    _walk_served(collect)
    return keys


def _walk_served(visit: Callable[[object], None]) -> None:
    """Depth-first over the real app's production routes.

    Test-added routes (a door probe mounting /boom on the shared app) are
    pollution, not service surface: only endpoints whose code object lives
    under ``crawlutron/`` count -- a mutant planted there is still caught.
    """
    from tests._app_factory import service_app

    app = service_app()
    assert app is not None

    def walk(routes: Sequence[object]) -> None:
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                inner = getattr(route, "original_router", None)
                walk(getattr(inner, "routes", ()))
                continue
            endpoint = getattr(route, "endpoint", None)
            code = getattr(endpoint, "__code__", None)
            if code is not None and "crawlutron/" in code.co_filename:
                visit(route)

    walk(app.router.routes)


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


def test_every_served_route_is_guarded_or_exempt():
    """The converse: nothing the app serves escapes the layer silently.

    served - guarded - exempt must be empty. The #664 PENDING table was sealed
    at zero (#668): every previously-pending surface meters through the
    anonymous bucket (api/anonymous_throttle.py), so the table and its pins
    are gone -- this unconditional assertion is the converse guard.
    """
    guarded = _guarded_keys()
    exempt = {(row.method, row.path.split(":")[0]) for row in LAYER_EXEMPT_RATCHET}
    anonymous = {(row.method, row.path) for row in ANONYMOUS_METERED}
    # Starlette derives HEAD from every GET route, so HEAD mirrors GET here.
    raw = _served_routes()
    served = raw - {("head", path) for _, path in raw} | {
        ("get", path) for method, path in raw if method == "head"
    }
    ungoverned = []
    for method, path in sorted(served - guarded - exempt - anonymous):
        for route in _routes_with_key((method, path)):
            ungoverned.append(f"{route}:{method.upper()} {path}")
    assert not ungoverned, (
        "served routes outside the auth+metering layer and the exemption ratchet "
        "(land its metering; the PENDING table is sealed shut at zero -- a new "
        "unmetered surface needs an owner-reviewed row): " + ", ".join(ungoverned)
    )


def test_anonymous_rows_match_real_routes():
    """A stale anonymous-metered row (renamed or deleted route) fails the gate."""
    routes = _served_routes()
    for row in ANONYMOUS_METERED:
        key = (row.method, row.path)
        assert key in routes, f"anonymous-metered row {key} no longer matches a served route"


def test_anonymous_ratchet_never_increases():
    """The pinned literal only moves down, in the commit that earns it."""
    assert len(ANONYMOUS_METERED) == 4, (
        "ANONYMOUS_METERED is pinned at 4; adding a row demands the metering "
        "landed in the same commit and this literal lowered/raised deliberately"
    )


def test_anonymous_rows_carry_a_justification():
    """A metered-surface declaration without a stated reason is not an explicit act."""
    for row in ANONYMOUS_METERED:
        assert isinstance(row, ExemptRow) and len(row.reason) > 20, f"row {row} lacks a real justification"


def _routes_with_key(key: tuple[str, str]) -> list[str]:
    """The source file+line of every served route definition matching key."""
    hits: list[str] = []

    def collect(route: object) -> None:
        if not isinstance(route, APIRoute):
            path, methods = getattr(route, "path", None), getattr(route, "methods", None)
            if path == key[1] and methods and key[0] in {m.lower() for m in methods}:
                hits.append(repr(getattr(route, "endpoint", None)))
            return
        for method in route.methods or ():
            if (method.lower(), route.path) == key:
                hits.append(_offender_module(route))

    _walk_served(collect)
    return [h for h in hits if h and h != "None"]


def test_every_row_carries_a_justification():
    """An exemption without a stated reason is not an explicit act."""
    for row in LAYER_EXEMPT_RATCHET:
        assert isinstance(row, ExemptRow) and len(row.reason) > 20, f"row {row} lacks a real justification"
