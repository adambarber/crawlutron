"""Fail-closed boot validation of the layer-exempt ratchet (#818).

Every declared row must be a mounted, credential-free door, or boot refuses naming the drifted rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.routing import APIRoute


def collect_mounted(routes: Sequence[object], mounted: dict[tuple[str, str], APIRoute]) -> None:
    """Walk mounted routes, descending into included routers."""
    from fastapi.routing import APIRoute

    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            inner = getattr(route, "original_router", None)
            if inner is not None:
                collect_mounted(getattr(inner, "routes", ()), mounted)
            continue
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            mounted.setdefault((method.lower(), route.path), route)


def validate_exempt_routes(app: FastAPI) -> None:
    """Fail closed when a declared exemption row has no mounted, credential-free door.

    guard_request resolves anonymous callers; a missing or guarded declared route refuses to boot.
    """
    from crawlutron.api.route_layer import ADMIN_GUARD_NAME, LAYER_EXEMPT_RATCHET, OPERATOR_GUARD_NAME

    refusing = frozenset({ADMIN_GUARD_NAME, OPERATOR_GUARD_NAME})

    def _refuses_anonymous(route: APIRoute) -> bool:
        names = {dep.call.__name__ for dep in route.dependant.dependencies if dep.call is not None}
        return bool(names & refusing)

    mounted: dict[tuple[str, str], APIRoute] = {}
    collect_mounted(app.router.routes, mounted)

    offenders: list[str] = []
    for row in LAYER_EXEMPT_RATCHET:
        path = row.path.split(":")[0]
        route = mounted.get((row.method, path))
        if route is None:
            offenders.append(f"{row.method.upper()} {path}: no mounted route")
        elif _refuses_anonymous(route):
            offenders.append(f"{row.method.upper()} {path}: behind a credential-refusing guard")
    if offenders:
        raise RuntimeError(
            "LAYER_EXEMPT_RATCHET rows without a mounted credential-free door: " + "; ".join(offenders)
        )


__all__ = ["validate_exempt_routes"]
