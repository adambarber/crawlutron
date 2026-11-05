"""The served OpenAPI declares the refusals each route can actually raise.

A wrong key is refused everywhere (401); a spent quota only where the route
spends one (429), so the two are declared at their own levels.
"""

from __future__ import annotations

import pytest
from fastapi.dependencies.utils import get_typed_signature
from fastapi.routing import APIRoute

from crawlutron.api import crawl_router, identity_router, jobs_router, keys_router, watch_router
from crawlutron.api.caller_auth import get_caller_identity, get_keys_caller

_AUTH_DEPS = {get_caller_identity.__name__, get_keys_caller.__name__}
_AUTH_ROUTERS = (crawl_router, identity_router, jobs_router, keys_router, watch_router)

#: The MCP endpoint resolves the caller inside its handler, so it is not a
#: dependency route: it refuses a wrong key with a 401 all the same.
_HAND_ROLLED_AUTH_ROUTES = (("post", "/mcp"),)

#: The routes that spend caller quota; the admit() in their path is what refuses.
_QUOTA_ROUTES = frozenset(
    {
        ("post", "/v1/crawl"),
        ("post", "/v1/jobs"),
        ("post", "/v1/map"),
        ("post", "/v1/render"),
        ("post", "/v1/search"),
    }
)

#: A 429 that is not a quota refusal: the caller's watch-count cap.
_WATCH_LIMIT_ROUTE = ("post", "/v1/watches")


def _is_auth_route(route: APIRoute) -> bool:
    """Whether ``route`` resolves caller identity (and can refuse a wrong key)."""
    for param in get_typed_signature(route.endpoint).parameters.values():
        metadata = getattr(param.annotation, "__metadata__", ())
        for meta in metadata:
            dep_name = getattr(getattr(meta, "dependency", None), "__name__", None)
            if dep_name in _AUTH_DEPS:
                return True
    return False


def _auth_ops() -> list[tuple[str, str]]:
    """Every (method, path) that can refuse a wrong key."""
    ops = []
    for module in _AUTH_ROUTERS:
        for route in module.router.routes:
            if isinstance(route, APIRoute) and _is_auth_route(route):
                method = next(iter(route.methods or ()))
                ops.append((method.lower(), route.path))
    return ops + list(_HAND_ROLLED_AUTH_ROUTES)


def _spec_responses() -> dict[tuple[str, str], set[str]]:
    from crawlutron.main import app

    assert app is not None, "the lifespan-set app is absent; re-ground this spec walk"
    spec = app.openapi()
    return {
        (method, path): set(operation["responses"])
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
    }


@pytest.mark.asyncio
async def test_every_auth_route_declares_401(async_client):
    """Every caller-auth route documents the 401 a wrong key earns."""
    responses = _spec_responses()
    ops = _auth_ops()
    assert ops, "the caller-auth router surface is empty"
    for method, path in ops:
        codes = responses.get((method, path), set())
        assert "401" in codes, f"{method.upper()} {path} must declare 401"


@pytest.mark.asyncio
async def test_only_quota_spending_routes_declare_429(async_client):
    """A 429 is declared exactly where the route can spend a quota.

    Declaring it on a read-only route would promise a refusal the route
    cannot make; omitting it on a spending route would hide one.
    """
    responses = _spec_responses()
    declared = {op for op, codes in responses.items() if "429" in codes}
    assert declared == _QUOTA_ROUTES | {_WATCH_LIMIT_ROUTE}
