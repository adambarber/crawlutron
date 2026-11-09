"""The parity gate's reach is total: nothing the app mounts escapes the walk.

`test_rest_mcp_parity` walks ROUTER_MODULES only; a router added directly in
`main._include_routers` would serve routes the gate never sees. This gate
asserts the mounting inventory itself is closed: every router the app mounts
is accounted for by the registry, the factory builders, or the MCP router.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawlutron.api.router_identity import router_module
from crawlutron.api.router_registry import ROUTER_MODULES
from tests._app_factory import service_app

if TYPE_CHECKING:
    from fastapi import APIRouter

#: The routers the app legitimately mounts outside ROUTER_MODULES: factory
#: routers and the MCP router. A direct-mount addition joins this set or the registry.
EXPECTED_DIRECT_MOUNTS: tuple[type[APIRouter], ...] = ()

_DIRECT_FACTORY_ROUTERS = {
    "crawlutron.discovery.agent_hints",
    "crawlutron.discovery.capabilities",
    "crawlutron.discovery.well_known",
    "crawlutron.health.readiness",
    "crawlutron.mcp.router",
}

#: Framework-owned routes (FastAPI's default docs/metrics mounts); not ours to gate.
_FRAMEWORK_ROUTE_MODULES = frozenset({"starlette.routing", "fastapi.routing"})


def _router_module(router: object) -> str:
    """Test alias of the production router-identity resolver."""
    return router_module(router)


def _mounted_routers() -> list[tuple[str, int]]:
    """Every distinct router the app mounts, as (identity module, route count)."""
    app = service_app()
    routers: dict[str, int] = {}
    for route in app.router.routes:
        inner = getattr(route, "original_router", None)
        if inner is None:
            continue
        module = _router_module(inner)
        routers[module] = routers.get(module, 0) + 1
    return sorted(routers.items())


def test_every_mounted_router_is_accounted_for():
    """The registry + the named factory modules cover everything the app mounts."""
    registered = {_router_module(router) for router in ROUTER_MODULES}
    unaccounted = [
        module
        for module, _ in _mounted_routers()
        if module not in registered
        and module not in _DIRECT_FACTORY_ROUTERS
        and module not in _FRAMEWORK_ROUTE_MODULES
    ]
    assert not unaccounted, f"routers mounted outside the registry and factory list: {unaccounted}"


def test_registry_routers_are_all_mounted():
    """The converse: the registry is the whole registry -- nothing declared but unmounted."""
    registered = {_router_module(router) for router in ROUTER_MODULES}
    mounted = {module for module, _ in _mounted_routers()}
    missing = [m for m in registered if m not in mounted]
    assert not missing, f"registry routers not mounted by the app: {missing}"


def test_factory_router_set_matches_direct_mounts():
    """A factory router added in _include_routers must join the declared set."""
    factory_modules = {m for m, _ in _mounted_routers() if m in _DIRECT_FACTORY_ROUTERS}
    assert factory_modules == _DIRECT_FACTORY_ROUTERS, (
        f"factory-router drift: missing {sorted(_DIRECT_FACTORY_ROUTERS - factory_modules)}, "
        f"extra {sorted(factory_modules - _DIRECT_FACTORY_ROUTERS)}"
    )
    assert not EXPECTED_DIRECT_MOUNTS
