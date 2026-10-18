"""Inbound caller identity: the key on the request, or anonymity.

A key arrives as ``X-API-Key`` or a Bearer token; no key is the anonymous
caller, a wrong key is a 401. Ownership elsewhere is an exact id match.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from typing import NoReturn

from fastapi import Request

from app.api.exceptions import http_error
from app.models.caller_identity import CallerIdentity
from app.models.caller_key import CallerKey
from app.models.json_value import JsonValue
from app.transport.request_profile import cookie_namespace_for


def identity_for_key(record: CallerKey) -> CallerIdentity:
    """The identity a verified key crawls as, quotas included."""
    return CallerIdentity(
        caller_id=record.key_id,
        label=record.label,
        authenticated=True,
        max_pages=record.max_pages,
        max_concurrency=record.max_concurrency,
    )


def anonymous_caller() -> CallerIdentity:
    """The shared anonymous bucket: a bounded single-user identity; quotas resolve to
    capability defaults (max_pages=100, max_concurrency=4), identical to a keyed caller
    that omits limits.
    """
    return CallerIdentity()


def presented_key(request: Request) -> str | None:
    """The caller's key from the header or Bearer token, if one was sent."""
    header = request.headers.get("x-api-key", "").strip()
    if header:
        return header
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _raise_invalid_api_key() -> NoReturn:
    """Shared 401 for any failed /v1/keys credential."""
    raise http_error(401, "invalid_api_key", "Unknown or revoked API key")


def _key_admin_token(request: Request) -> str:
    """The configured operator token, or '' when unset."""
    state = getattr(request, "app", None)
    state = getattr(state, "state", None) if state is not None else None
    if state is not None:
        token = getattr(state, "key_admin_token", None)
        if isinstance(token, str) and token:
            return token
        settings = getattr(state, "crawler_settings", None)
        if settings is not None:
            token = getattr(settings, "key_admin_token", None)
            if isinstance(token, str) and token:
                return token
    return os.environ.get("CRAWLUTRON_KEY_ADMIN_TOKEN", "")


async def get_keys_caller(request: Request) -> CallerIdentity:
    """FastAPI dependency: a verified caller or the operator for /v1/keys."""
    secret = presented_key(request)
    if not secret:
        raise http_error(401, "invalid_api_key", "Unknown or revoked API key")
    admin_token = _key_admin_token(request)
    if admin_token and secrets.compare_digest(secret, admin_token):
        return CallerIdentity(
            caller_id="admin",
            label="admin",
            authenticated=True,
            max_pages=0,
            max_concurrency=0,
        )
    return await _caller_from_secret(request, secret)


async def _caller_from_secret(request: Request, secret: str) -> CallerIdentity:
    """Resolve a presented secret to an identity or raise 401."""
    store = getattr(request.app.state, "caller_key_store", None)
    record = await store.averify_key(secret) if store is not None else None
    if record is None:
        _raise_invalid_api_key()
    return identity_for_key(record)


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency: who this request crawls as."""
    secret = presented_key(request)
    if not secret:
        return anonymous_caller()
    return await _caller_from_secret(request, secret)


def is_operator(request: Request) -> bool:
    """Whether the presented credential is the configured operator token.

    Soft gate, not a 401: ``/metrics`` keeps its base exposition loopback-accessible
    and only per-caller detail needs the token (constant-time compare).
    """
    secret = presented_key(request)
    if not secret:
        return False
    admin_token = _key_admin_token(request)
    return bool(admin_token) and secrets.compare_digest(secret, admin_token)


def effective_cookie_namespace(profile: Mapping[str, JsonValue], caller: CallerIdentity) -> str:
    """The cookie namespace for one crawl: an explicit profile wins, else the key."""
    if caller.authenticated and caller.caller_id:
        return cookie_namespace_for(profile) or caller.caller_id
    return cookie_namespace_for(profile)


def resolve_page_limit(caller: CallerIdentity, requested_max_pages: int) -> int:
    """Resolve an omitted (zero) page limit to the caller's bound.

    One path for every admission edge: a positive request stands; an omitted
    request takes the caller's bound, else stays unbounded. ``#194`` extends.
    """
    if requested_max_pages > 0:
        return requested_max_pages
    return caller.max_pages if caller.max_pages > 0 else 0


def check_page_quota(caller: CallerIdentity, requested_max_pages: int) -> None:
    """Refuse a crawl past the caller's page budget with a 429."""
    if caller.max_pages > 0 and requested_max_pages > caller.max_pages:
        raise http_error(
            429,
            "quota_exceeded",
            f"Caller page quota is {caller.max_pages}, requested {requested_max_pages}",
        )


def check_concurrency_quota(caller: CallerIdentity, active_count: int) -> None:
    """Refuse when the caller's live jobs already fill its concurrency budget."""
    if caller.max_concurrency > 0 and active_count >= caller.max_concurrency:
        raise http_error(
            429,
            "quota_exceeded",
            f"Caller concurrency quota is {caller.max_concurrency}, {active_count} already active",
        )


def active_sync_for(caller: CallerIdentity, request: Request) -> int:
    """The caller's live sync crawl count, from the per-process in-flight register."""
    tracker = getattr(request.app.state, "concurrency_tracker", None)
    return tracker.sync_in_flight(caller.caller_id) if tracker is not None else 0


__all__ = [
    "active_sync_for",
    "anonymous_caller",
    "check_concurrency_quota",
    "check_page_quota",
    "effective_cookie_namespace",
    "get_caller_identity",
    "get_keys_caller",
    "identity_for_key",
    "is_operator",
    "presented_key",
    "resolve_page_limit",
]
