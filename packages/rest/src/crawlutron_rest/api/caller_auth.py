"""Inbound caller identity: the key on the request, or anonymity.

The caller-auth CORE (quota law, admit, the cookie-namespace binding) lives in
:mod:`crawlutron.service.caller_auth`; this module keeps the Request plumbing.
"""

from __future__ import annotations

from typing import NoReturn, TypedDict

from fastapi import Request

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.error_response import ErrorResponse
from crawlutron.service.caller_auth import (
    Tracking,
    admit,
    anonymous_caller,
    bound_render_cookie_namespace,
    check_concurrency_quota,
    check_page_quota,
    effective_cookie_namespace,
    identity_for_key,
    operator_caller,
    refuse_anonymous_cookie_namespace,
    resolve_page_limit,
)
from crawlutron.service.errors import AuthRefusal
from crawlutron_rest.api.credentials import constant_time_eq, is_operator, key_admin_token, presented_key
from crawlutron_rest.api.throttle import shared_throttle


async def record_operator_auth_failure(audit, *, actor: str = "anonymous") -> None:
    """One durable audit row per failed operator auth (#697). No-op when
    no audit store is wired. Runs off the loop via arecord_event so
    txn_write's off-loop assertion never trips on an async handler."""
    if audit is None:
        return
    await audit.arecord_event(
        actor=actor,
        action="operator_auth_failure",
        target_key_id=None,
        outcome="refused",
    )


async def _raise_invalid_api_key() -> NoReturn:
    """Shared 401 for any failed /v1/keys credential."""
    from crawlutron.runtime.metrics import get_metrics

    await get_metrics().record_auth_refusal()
    raise AuthRefusal("invalid_api_key", "Unknown or revoked API key")


#: The refusal declarations an auth-bearing route carries. Both are raised in
#: dependency body, so FastAPI cannot infer them from the handler signature.
class _ResponseSpec(TypedDict):
    """FastAPI ``responses`` entry: the wire error model plus its description."""

    model: type[ErrorResponse]
    description: str


#: Every auth-bearing route refuses a wrong or revoked key.
AUTH_DENIAL: dict[int | str, _ResponseSpec] = {
    401: {"model": ErrorResponse, "description": "Unknown or revoked API key."},
}

#: Only a route that spends quota can be told the quota is spent.
QUOTA_DENIAL: dict[int | str, _ResponseSpec] = {
    429: {"model": ErrorResponse, "description": "Caller page or concurrency quota exceeded."},
}


def auth_denials() -> dict[int | str, dict[str, object]]:
    """The 401 declaration in the response-map shape FastAPI reads.

    Declared on the router, so every route it guards documents the refusal.
    """
    return {k: dict(v) for k, v in AUTH_DENIAL.items()}


def quota_denials() -> dict[int | str, dict[str, object]]:
    """The 429 declaration, for the routes that spend quota."""
    return {k: dict(v) for k, v in QUOTA_DENIAL.items()}


async def get_keys_caller(request: Request) -> CallerIdentity:
    """FastAPI dependency: the operator token is the only /v1/keys caller.

    A caller key never enters; an unconfigured token seals it (fail closed).
    Failed guesses meter through the shared throttle's ``admin`` bucket (#659).
    """
    secret = presented_key(request)
    admin_token = key_admin_token(request)
    if not secret or not admin_token:
        await _raise_invalid_api_key()
    throttle = shared_throttle()
    await throttle.acheck_admin()
    if not constant_time_eq(secret, admin_token):
        await throttle.arecord_admin_failure()
        await record_operator_auth_failure(_audit_store(request))
        await _raise_invalid_api_key()
    await throttle.arecord_admin_success()
    return operator_caller()


async def _caller_from_secret(request: Request, secret: str) -> CallerIdentity:
    """Resolve a presented secret to an identity or raise 401.

    The meter runs BEFORE the PBKDF2, keying the full secret PLUS the remote IP
    (#929): a cooldown refuses without another derivation (#653).
    """
    throttle = shared_throttle()
    remote = request.client.host if request.client else None
    await throttle.acheck(secret, remote)
    store = getattr(request.app.state, "caller_key_store", None)
    record = await store.averify_key(secret) if store is not None else None
    if record is None:
        await throttle.arecord_failure(secret, remote)
        # A non-key secret also paces the shared admin bucket (#826): guesses arm
        # the cooldown /v1/keys enforces. Valid keys never reach this refusal arm.
        await throttle.arecord_admin_failure()
        await _raise_invalid_api_key()
    await throttle.arecord_success(secret, remote)
    return identity_for_key(record)


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency: who this request crawls as.

    The configured operator token resolves to the operator identity (fail-closed
    when unset); any other presented secret rides the keyed path unchanged."""
    secret = presented_key(request)
    if not secret:
        return anonymous_caller()
    if verify_admin_secret(secret, request):
        await shared_throttle().arecord_admin_success()
        return operator_caller()
    return await _caller_from_secret(request, secret)


def _audit_store(request: Request):
    """The app's audit trail, or None when unwired (fail-open recording only:
    an audit outage must not take the auth door down)."""
    return getattr(getattr(request, "app", None), "state", None) and getattr(
        request.app.state, "audit_store", None
    )


def verify_admin_secret(secret: str, request: Request) -> bool:
    """Constant-time admin-token check, public for the layer's soft gate."""
    admin_token = key_admin_token(request)
    return bool(admin_token) and constant_time_eq(secret, admin_token)


__all__ = [
    "AUTH_DENIAL",
    "QUOTA_DENIAL",
    "Tracking",
    "admit",
    "anonymous_caller",
    "auth_denials",
    "bound_render_cookie_namespace",
    "check_concurrency_quota",
    "check_page_quota",
    "effective_cookie_namespace",
    "get_caller_identity",
    "get_keys_caller",
    "identity_for_key",
    "is_operator",
    "operator_caller",
    "presented_key",
    "quota_denials",
    "record_operator_auth_failure",
    "refuse_anonymous_cookie_namespace",
    "resolve_page_limit",
    "verify_admin_secret",
]
