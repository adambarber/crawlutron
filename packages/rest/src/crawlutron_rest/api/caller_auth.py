"""Inbound caller identity: the key on the request, or anonymity.

The caller-auth CORE (quota law, admit, the cookie-namespace binding) lives in
:mod:`crawlutron.service.caller_auth`; this module keeps the Request plumbing.
"""

from __future__ import annotations

import secrets
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
from crawlutron.service.key_throttle import shared_throttle


def _presented_key(request: Request) -> str | None:
    """The caller's key from the header or Bearer token, if one was sent."""
    header = request.headers.get("x-api-key", "").strip()
    if header:
        return header
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def presented_key(request: Request) -> str | None:
    """The one credential-parsing funnel's public door: every guard that extracts
    a presented secret reads it here, never re-parses headers itself."""
    return _presented_key(request)


def record_operator_auth_failure(audit, *, actor: str = "anonymous") -> None:
    """One durable audit row per failed operator authentication (#697); the #696
    counter stays the metric, this the durable record -- never both wired.
    No-op when no audit store is wired (tests)."""
    if audit is None:
        return
    audit.record_event(actor=actor, action="operator_auth_failure", target_key_id=None, outcome="refused")


def _raise_invalid_api_key() -> NoReturn:
    """Shared 401 for any failed /v1/keys credential."""
    from crawlutron.runtime.metrics import get_metrics

    get_metrics().record_auth_refusal()
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


def key_admin_token(request: Request) -> str:
    """The configured operator token, or '' when unset -- ONE home: the settings
    object stashed on app.state (#666). No env fallback, no memo: a memoized
    second home authenticated a rotated-away token for the process lifetime.
    """
    app = request.scope.get("app")
    state = getattr(app, "state", None) if app is not None else None
    settings = getattr(state, "crawler_settings", None) if state is not None else None
    token = getattr(settings, "key_admin_token", None)
    return token if isinstance(token, str) else ""


def _constant_time_eq(secret: str, token: str) -> bool:
    """Constant-time compare that tolerates non-ASCII str.

    ``secrets.compare_digest`` raises TypeError on non-ASCII str, which would
    500 a keys-door denial; encoding both sides to bytes fixes it.
    """
    return (
        bool(secret) and bool(token) and secrets.compare_digest(secret.encode("utf-8"), token.encode("utf-8"))
    )


async def get_keys_caller(request: Request) -> CallerIdentity:
    """FastAPI dependency: the operator token is the only /v1/keys caller.

    A caller key never enters; an unconfigured token seals it (fail closed).
    Failed guesses meter through the shared throttle's ``admin`` bucket (#659).
    """
    secret = _presented_key(request)
    admin_token = key_admin_token(request)
    if not secret or not admin_token:
        _raise_invalid_api_key()
    throttle = shared_throttle()
    throttle.check_admin()
    if not _constant_time_eq(secret, admin_token):
        throttle.record_admin_failure()
        record_operator_auth_failure(_audit_store(request))
        _raise_invalid_api_key()
    throttle.record_admin_success()
    return operator_caller()


async def _caller_from_secret(request: Request, secret: str) -> CallerIdentity:
    """Resolve a presented secret to an identity or raise 401.

    The meter runs BEFORE the PBKDF2, keying the full secret PLUS the remote IP
    (#929): a cooldown refuses without another derivation (#653).
    """
    throttle = shared_throttle()
    remote = request.client.host if request.client else None
    throttle.check(secret, remote)
    store = getattr(request.app.state, "caller_key_store", None)
    record = await store.averify_key(secret) if store is not None else None
    if record is None:
        throttle.record_failure(secret, remote)
        _raise_invalid_api_key()
    throttle.record_success(secret, remote)
    return identity_for_key(record)


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency: who this request crawls as.

    The configured operator token resolves to the operator identity (fail-closed
    when unset); any other presented secret rides the keyed path unchanged."""
    secret = _presented_key(request)
    if not secret:
        return anonymous_caller()
    if verify_admin_secret(secret, request):
        shared_throttle().record_admin_success()
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
    return bool(admin_token) and _constant_time_eq(secret, admin_token)


def is_operator(request: Request) -> bool:
    """Whether the presented credential is the configured operator token.

    Soft gate, not a 401: ``/metrics`` keeps its base exposition loopback-accessible
    and only per-caller detail needs the token (constant-time compare).
    """
    secret = _presented_key(request)
    if not secret:
        return False
    admin_token = key_admin_token(request)
    return bool(admin_token) and _constant_time_eq(secret, admin_token)


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
    "key_admin_token",
    "operator_caller",
    "presented_key",
    "quota_denials",
    "record_operator_auth_failure",
    "refuse_anonymous_cookie_namespace",
    "resolve_page_limit",
    "verify_admin_secret",
]
