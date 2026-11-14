"""Every mounted route is governed: layer guard, handler auth, or a ratified
exemption; the walk covers every mounted door derived from the app (#1033).
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.routing import APIRoute

from crawlutron_rest.api.route_layer import (
    ADMIN_GUARD_NAME,
    GUARD_NAME,
    LAYER_EXEMPT_RATCHET,
    OPERATOR_GUARD_NAME,
)

_LAYER_GUARDS = frozenset({GUARD_NAME, ADMIN_GUARD_NAME, OPERATOR_GUARD_NAME})
_IDENTITY_DOOR = "get_caller_identity"

#: Declared-exempt (method, path) doors, ratified by the boot validator.
_EXEMPT_DOORS = frozenset((row.method, row.path.split(":")[0]) for row in LAYER_EXEMPT_RATCHET)


def _offender_module(route: APIRoute) -> str:
    """The source file the route's endpoint lives in, relative to the repo root.

    Reads the endpoint's own code object: the file+line the defect is at.
    """
    filename = route.endpoint.__code__.co_filename
    return filename.split("crawlutron/")[-1] if "crawlutron/" in filename else filename


def _guard_names(route: APIRoute) -> set[str]:
    """The dependency function names the route's dependant tree carries."""
    names: set[str] = set()
    for dep in route.dependant.dependencies:
        if dep.call is not None:
            names.add(dep.call.__name__)
    return names


def _route_is_layer_guarded(route: APIRoute) -> bool:
    """Whether the route's dependency set includes a layer guard."""
    return bool(_guard_names(route) & _LAYER_GUARDS)


def _route_authenticates_in_handler(route: APIRoute) -> bool:
    """Prove structurally: the endpoint's own code names the identity door.

    A comment claiming auth is not enough; ``co_names`` must reference
    ``get_caller_identity`` (or its successor).
    """
    code = getattr(route.endpoint, "__code__", None)
    return code is not None and _IDENTITY_DOOR in code.co_names


def _route_is_declared_exempt(route: APIRoute) -> bool:
    """Whether the route is a ratified layer-exempt door, not an offender."""
    return any((method.lower(), route.path) in _EXEMPT_DOORS for method in (route.methods or ()))


def _mounted_api_routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute served by a mounted router, descending into included routers.

    Reads the served surface from ``app`` instead of a hand-typed tuple.
    """
    routes: list[APIRoute] = []

    def walk(route_list: Sequence[object]) -> None:
        for route in route_list:
            if type(route).__name__ == "_IncludedRouter":
                inner = getattr(route, "original_router", None)
                if inner is not None:
                    walk(getattr(inner, "routes", ()))
                continue
            if isinstance(route, APIRoute):
                routes.append(route)

    walk(app.router.routes)
    return routes


def _unguarded_routes(app: FastAPI) -> list[tuple[str, int, str]]:
    """Mounted routes outside the layer, handler auth, and the exemption ratchet.

    Each offender names its own file:line; default closed, never skipped.
    """
    offenders: list[tuple[str, int, str]] = []
    for route in _mounted_api_routes(app):
        if _route_is_layer_guarded(route):
            continue
        if _route_authenticates_in_handler(route):
            continue
        if _route_is_declared_exempt(route):
            continue
        offenders.append((_offender_module(route), route.endpoint.__code__.co_firstlineno, route.path))
    return offenders


def test_every_mounted_route_is_guarded_or_handler_authenticated():
    """Every mounted door is governed; /mcp (handler-auth) is now walked (#1033).

    The old walk read ROUTER_MODULES only and never saw the MCP door.
    """
    from tests._app_factory import service_app

    app = service_app()
    walked = _mounted_api_routes(app)
    assert any(route.path == "/mcp" for route in walked), "the walk no longer covers the mounted MCP door"
    offenders = _unguarded_routes(app)
    assert not offenders, (
        "mounted routes outside the auth+metering layer, handler authentication, "
        "and the exemption ratchet: " + "; ".join(f"{mod}:{line} {path}" for mod, line, path in offenders)
    )
