"""Inbound caller identity: the key on the request, or anonymity.

A key arrives as ``X-API-Key`` or a Bearer token; no key is the anonymous
caller, a wrong key is a 401. Ownership elsewhere is an exact id match.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    """The identity of a request that presented no key: fresh per call."""
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


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency: who this request crawls as."""
    secret = presented_key(request)
    if not secret:
        return anonymous_caller()
    store = getattr(request.app.state, "caller_key_store", None)
    record = await store.averify_key(secret) if store is not None else None
    if record is None:
        raise http_error(401, "invalid_api_key", "Unknown or revoked API key")
    return identity_for_key(record)


def effective_cookie_namespace(profile: Mapping[str, JsonValue], caller: CallerIdentity) -> str:
    """The cookie namespace for one crawl: an explicit profile wins, else the key."""
    if caller.authenticated and caller.caller_id:
        return cookie_namespace_for(profile) or caller.caller_id
    return cookie_namespace_for(profile)


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


__all__ = [
    "anonymous_caller",
    "check_concurrency_quota",
    "check_page_quota",
    "effective_cookie_namespace",
    "get_caller_identity",
    "identity_for_key",
    "presented_key",
]
