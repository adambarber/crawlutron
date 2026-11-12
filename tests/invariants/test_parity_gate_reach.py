"""The parity gate's reach is total: nothing the app mounts escapes the walk. ``test_rest_mcp_parity`` walks
ROUTER_MODULES only; a router added directly in ``main._include_routers`` would serve routes the gate
never sees.
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
    "crawlutron.api.mcp_router",
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


def _mounted_router_copies() -> list[tuple[str, object]]:
    """Every mounted route's (identity module, backing router object), with repeats."""
    app = service_app()
    return [
        (_router_module(inner), inner)
        for route in app.router.routes
        if (inner := getattr(route, "original_router", None)) is not None
    ]


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


def test_each_module_is_backed_by_one_router_object():
    """A module served by two distinct router objects is a double-mount (factory called twice)."""
    backing: dict[str, set[int]] = {}
    for module, router in _mounted_router_copies():
        backing.setdefault(module, set()).add(id(router))
    duplicated = {m: len(ids) for m, ids in backing.items() if len(ids) != 1}
    assert not duplicated, f"modules mounted via multiple distinct router objects: {duplicated}"


def test_each_router_object_is_mounted_exactly_once():
    """One route-object entry per mounted module; a second entry is a re-mounted router."""
    counts: dict[str, int] = {}
    for module, _ in _mounted_router_copies():
        counts[module] = counts.get(module, 0) + 1
    remounted = {m: n for m, n in counts.items() if n != 1}
    assert not remounted, f"router objects mounted more than once: {remounted}"
