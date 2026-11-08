"""The auth+metering layer (#664): caller identity plus the readiness gate.

Every feature route carries these by default; the routers are built through
:func:`guarded_router` so the guard cannot be omitted by accident.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from crawlutron.api.caller_auth import auth_denials, get_keys_caller, is_operator
from crawlutron.api.dependencies import CallerDep
from crawlutron.api.key_throttle import shared_throttle
from crawlutron.models.caller_identity import CallerIdentity

#: The resolver names the invariant walk matches route dependencies against.
GUARD_NAME = "guard_request"
ADMIN_GUARD_NAME = "guard_admin_request"
OPERATOR_GUARD_NAME = "operator_guard"

_LAYER_GUARDS = frozenset({GUARD_NAME, ADMIN_GUARD_NAME, OPERATOR_GUARD_NAME})


async def guard_request(caller: CallerDep) -> CallerIdentity:
    """The layer: resolve the caller (401 on a bad key) before the handler."""
    return caller


async def guard_admin_request(request: Request) -> None:
    """The operator trust plane: the token is the only credential (fail closed)."""
    await get_keys_caller(request)


async def operator_guard(request: Request) -> bool:
    """The soft operator gate: negatives pace through the admin bucket."""
    secret_presented = is_operator(request)
    if not secret_presented:
        throttle = shared_throttle()
        throttle.check_admin()
    return secret_presented


def guarded_router(**kwargs: Any) -> APIRouter:
    """A feature router with the layer guard and the 401 declaration wired."""
    return APIRouter(dependencies=[Depends(guard_request)], responses=auth_denials(), **kwargs)


def admin_guarded_router(**kwargs: Any) -> APIRouter:
    """The operator trust plane's router: the admin guard, fail closed."""
    return APIRouter(dependencies=[Depends(guard_admin_request)], responses=auth_denials(), **kwargs)


__all__ = [
    "ADMIN_GUARD_NAME",
    "GUARD_NAME",
    "OPERATOR_GUARD_NAME",
    "admin_guarded_router",
    "guard_admin_request",
    "guard_request",
    "guarded_router",
    "operator_guard",
]
