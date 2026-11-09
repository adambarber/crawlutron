"""The auth+metering layer (#664): caller identity plus the readiness gate.

Every feature route carries these by default; the routers are built through
:func:`guarded_router` so the guard cannot be omitted by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from crawlutron.api.caller_auth import auth_denials, get_keys_caller
from crawlutron.api.dependencies import CallerDep
from crawlutron.api.key_throttle import shared_throttle
from crawlutron.models.caller_identity import CallerIdentity

#: The resolver names the invariant walk matches route dependencies against.
GUARD_NAME = "guard_request"
ADMIN_GUARD_NAME = "guard_admin_request"
OPERATOR_GUARD_NAME = "operator_guard"

_LAYER_GUARDS = frozenset({GUARD_NAME, ADMIN_GUARD_NAME, OPERATOR_GUARD_NAME})

#: The public door the invariant walks read: every resolver name the layer owns.
LAYER_GUARDS: frozenset[str] = _LAYER_GUARDS


async def guard_request(caller: CallerDep) -> CallerIdentity:
    """The layer: resolve the caller (401 on a bad key) before the handler."""
    return caller


async def guard_admin_request(request: Request) -> None:
    """The operator trust plane: the token is the only credential (fail closed)."""
    await get_keys_caller(request)


async def operator_guard(request: Request) -> bool:
    """The soft operator gate: negatives pace through the admin bucket.

    No token presented stays anonymous without touching the meter; a presented
    token is paced BEFORE the compare and its outcome is recorded, mirroring
    the keys door (#659) while keeping the soft 200-on-miss contract (#209).
    """
    from crawlutron.api.caller_auth import verify_admin_secret

    secret = request.headers.get("x-api-key", "").strip()
    if not secret:
        return False
    throttle = shared_throttle()
    throttle.check_admin()
    matched = verify_admin_secret(secret, request)
    if matched:
        throttle.record_admin_success()
    else:
        throttle.record_admin_failure()
    return matched


def guarded_router(**kwargs: Any) -> APIRouter:
    """A feature router with the layer guard and the 401 declaration wired."""
    return APIRouter(dependencies=[Depends(guard_request)], responses=auth_denials(), **kwargs)


def admin_guarded_router(**kwargs: Any) -> APIRouter:
    """The operator trust plane's router: the admin guard, fail closed."""
    return APIRouter(dependencies=[Depends(guard_admin_request)], responses=auth_denials(), **kwargs)


@dataclass(frozen=True)
class ExemptRow:
    """One declared layer exemption, with the justification the owner reviews."""

    method: str
    path: str
    reason: str


#: The pinned ratchet: adding a row is RED until the owner accepts the
#: justification; the list and its count move DOWN only, same commit.
LAYER_EXEMPT_RATCHET: tuple[ExemptRow, ...] = (
    ExemptRow(
        "get",
        "/health",
        "Liveness probe for local orchestration (Docker healthcheck); a pure, "
        "I/O-free read that must answer before any identity store exists.",
    ),
    ExemptRow(
        "get",
        "/ready",
        "Readiness probe; the saturation report is the orchestrator's backpressure "
        "signal and must not require a credential to read.",
    ),
    ExemptRow(
        "get",
        "/.well-known/crawler-service",
        "Machine-readable discovery: agent integrations are REQUIRED to negotiate "
        "through this surface first (AGENTS.md), so it must answer unauthenticated.",
    ),
    ExemptRow(
        "get",
        "/v1/capabilities",
        "Capability advertisement for agent negotiation; a pure settings read with "
        "no caller-scoped data and no I/O.",
    ),
    ExemptRow(
        "get",
        "/v1/agent-hints",
        "Static agent guidance payload; pure, no store, no egress.",
    ),
    ExemptRow(
        "get",
        "/openapi.json",
        "The API contract itself; tools generate clients from it before holding a key.",
    ),
    ExemptRow(
        "post",
        "/v1/admit:check_robots=false",
        "The PURE base admission check is I/O-free by construction (no fetch, no "
        "egress, no store) and is the recommended pre-flight for first-time agents; "
        "the robots arm (check_robots=true) is NOT exempt -- it meters through the "
        "caller's identity and the key-verify throttle.",
    ),
)
EXEMPT_ROW_COUNT: int = 7


__all__ = [
    "ADMIN_GUARD_NAME",
    "EXEMPT_ROW_COUNT",
    "GUARD_NAME",
    "LAYER_EXEMPT_RATCHET",
    "LAYER_GUARDS",
    "OPERATOR_GUARD_NAME",
    "ExemptRow",
    "admin_guarded_router",
    "guard_admin_request",
    "guard_request",
    "guarded_router",
    "operator_guard",
]
