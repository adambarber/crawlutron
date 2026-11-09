"""Router identity: the module whose endpoint code a router's routes carry."""

from __future__ import annotations

__all__ = ["router_module"]


def router_module(router: object) -> str:
    """One router's identity module; ambiguous routers fall back to the class."""
    modules = {
        route.endpoint.__module__
        for route in getattr(router, "routes", ())
        if getattr(route, "endpoint", None) is not None
    }
    return next(iter(modules)) if len(modules) == 1 else type(router).__module__
