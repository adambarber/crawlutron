"""The surface walk behind recommended_endpoints: registry routes + discovery doors."""

from __future__ import annotations

#: Factory-mounted caller-facing doors the registry does not hold, in the
#: advertisement order the well-known payload has always presented.
DISCOVERY_DOORS: tuple[str, ...] = (
    "GET /health",
    "GET /ready",
    "GET /v1/capabilities",
    "GET /v1/agent-hints",
    "POST /mcp",
)

#: The operator trust plane: live routes deliberately outside the caller-facing
#: recommendation list (the keys door is reachable; it is not self-served).
OPERATOR_EXCLUSIONS: frozenset[str] = frozenset(
    {"GET /metrics", "POST /v1/keys", "GET /v1/keys", "DELETE /v1/keys/{key_id}"}
)

#: Registry modules first in the recommendation list, in agent-use order
#: (pre-flight admission, then the fetch primitives); anything else appended.
_ADVERTISEMENT_ORDER = (
    "crawlutron_rest.api.admit_router",
    "crawlutron_rest.api.crawl_router",
    "crawlutron_rest.api.identity_router",
    "crawlutron_rest.api.jobs_router",
    "crawlutron_rest.api.keys_router",
    "crawlutron_rest.api.watch_router",
    "crawlutron_rest.api.metrics_router",
    "crawlutron_rest.api.policy_router",
)

__all__ = ["DISCOVERY_DOORS", "OPERATOR_EXCLUSIONS", "advertised_route_strings"]


def _registry_route_strings() -> list[str]:
    """Every registry (METHOD path) string, advertisement order first."""
    from fastapi.routing import APIRoute

    from crawlutron_rest.api.router_identity import router_module
    from crawlutron_rest.api.router_registry import ROUTER_MODULES

    modules = (router_module(r) for r in ROUTER_MODULES)
    routers = dict(zip(modules, ROUTER_MODULES, strict=True))
    ordered = [routers[name] for name in _ADVERTISEMENT_ORDER if name in routers]
    ordered += [r for r in ROUTER_MODULES if r not in ordered]
    routes: list[str] = []
    for router in ordered:
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in sorted(route.methods or ()):
                    routes.append(f"{method.upper()} {route.path}")
    return routes


def advertised_route_strings() -> list[str]:
    """The caller-facing surface: registry routes plus the discovery doors."""
    return [
        route for route in (*DISCOVERY_DOORS, *_registry_route_strings()) if route not in OPERATOR_EXCLUSIONS
    ]
