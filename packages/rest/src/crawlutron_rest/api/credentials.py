"""Credential parsing and the operator-token gate, owned below both callers.

`throttle` sits under `caller_auth`; putting these here keeps the middleware
able to answer "is this a credential?" without an import cycle.
"""

from __future__ import annotations

import secrets

from fastapi import Request

__all__ = ["constant_time_eq", "is_operator", "key_admin_token", "presented_key", "remote_host"]


def presented_key(request: Request) -> str | None:
    """The caller's key from the header or Bearer token, if one was sent."""
    header = request.headers.get("x-api-key", "").strip()
    if header:
        return header
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def key_admin_token(request: Request) -> str:
    """The configured operator token, or '' when unset -- ONE home: app.state."""
    app = request.scope.get("app")
    state = getattr(app, "state", None) if app is not None else None
    settings = getattr(state, "crawler_settings", None) if state is not None else None
    token = getattr(settings, "key_admin_token", None)
    return token if isinstance(token, str) else ""


def constant_time_eq(secret: str, token: str) -> bool:
    """Constant-time compare that tolerates non-ASCII str."""
    return (
        bool(secret) and bool(token) and secrets.compare_digest(secret.encode("utf-8"), token.encode("utf-8"))
    )


def is_operator(request: Request) -> bool:
    """Whether the presented credential is the configured operator token."""
    secret = presented_key(request)
    if not secret:
        return False
    admin_token = key_admin_token(request)
    return bool(admin_token) and constant_time_eq(secret, admin_token)


def remote_host(request: Request) -> str | None:
    """The request's remote address, or None when the transport provides none."""
    client = request.client
    return client.host if client else None
