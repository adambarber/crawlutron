"""The auth+metering layer invariant (#664): every feature route is guarded.

A route is guarded iff its own dependency list carries a ``Depends`` on the
layer's resolver; the walk names each offender's file and line.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from crawlutron_rest.api.route_layer import ADMIN_GUARD_NAME, GUARD_NAME, OPERATOR_GUARD_NAME
from crawlutron_rest.api.router_registry import ROUTER_MODULES

_LAYER_GUARDS = frozenset({GUARD_NAME, ADMIN_GUARD_NAME, OPERATOR_GUARD_NAME})


def _offender_module(route: APIRoute) -> str:
    """The source file the route's endpoint lives in, relative to the repo root.

    An instance's ``__module__`` names the class, so the walk reads the
    endpoint's own code object instead: the file+line the defect is at.
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


def _unguarded_routes() -> list[tuple[str, int, str]]:
    offenders: list[tuple[str, int, str]] = []
    for router in ROUTER_MODULES:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            if not _route_is_layer_guarded(route):
                offenders.append(
                    (_offender_module(route), route.endpoint.__code__.co_firstlineno, route.path)
                )
    return offenders


def test_every_feature_route_carries_the_layer_guard():
    """A route registered without the layer dependency is refused, naming file+line."""
    assert not _unguarded_routes(), (
        "routes registered without the auth+metering layer guard "
        f"({GUARD_NAME}): " + "; ".join(f"{mod}:{line} {path}" for mod, line, path in _unguarded_routes())
    )
