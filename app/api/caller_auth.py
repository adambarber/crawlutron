"""Inbound caller identity: the key on the request, or anonymity.

A key arrives as ``X-API-Key`` or a Bearer token; no key is the anonymous
caller, a wrong key is a 401. Ownership elsewhere is an exact id match.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from fastapi import Request

from app.api.exceptions import http_error
from app.models.caller_identity import CallerIdentity
from app.models.caller_key import CallerKey
from app.models.json_value import JsonValue
from app.transport.request_profile import cookie_namespace_for

if TYPE_CHECKING:
    from app.api.caller_concurrency import CallerConcurrencyTracker
    from app.models.crawl_request import CrawlRequest
    from app.models.map_request import MapRequest
    from app.storage.job_store import JobStore

    #: Every carrier of a mutable ``CrawlPolicy`` the admit helper writes the resolved
    #: page bound and cookie namespace into; crawl and map are the two today.
    PolicyCarrier = CrawlRequest | MapRequest


@dataclass(frozen=True)
class Tracking:
    """The per-request concurrency state one admit call reads and releases."""

    job_store: JobStore | None = None
    concurrency_tracker: CallerConcurrencyTracker | None = None


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
    """The cookie namespace for one crawl: for an authenticated caller, pinned to the key id
    (a caller-supplied override could claim another caller's key and share their jar); an anonymous
    caller keeps whatever the profile declares.
    """
    if caller.authenticated and caller.caller_id:
        return caller.caller_id
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


@asynccontextmanager
async def admit(
    caller: CallerIdentity | None,
    requested_pages: int,
    *,
    tracking: Tracking,
    payload: PolicyCarrier | None = None,
    reserve_slot: bool = True,
) -> AsyncIterator[str]:
    """Admit one request: resolve the page bound, the quotas, the cookie namespace.

    ``reserve_slot=False`` when a durable job row counts instead of a sync slot.
    """
    active = caller if caller is not None else anonymous_caller()
    resolved = resolve_page_limit(active, requested_pages)
    if payload is not None:
        policy = payload.policy.model_copy(
            update={"limits": payload.policy.limits.model_copy(update={"max_pages": resolved})}
        )
        namespace = effective_cookie_namespace(policy.request_profile, active)
        if namespace:
            profile = dict(policy.request_profile)
            profile["cookie_namespace"] = namespace
            policy = policy.model_copy(update={"request_profile": profile})
        payload.policy = policy
    check_page_quota(active, resolved)
    job_store = tracking.job_store
    concurrency_tracker = tracking.concurrency_tracker
    active_count = await job_store.aactive_count_for_caller(active.caller_id) if job_store is not None else 0
    if concurrency_tracker is not None:
        active_count += concurrency_tracker.sync_in_flight(active.caller_id)
    check_concurrency_quota(active, active_count)
    if reserve_slot and concurrency_tracker is not None:
        concurrency_tracker.reserve(active.caller_id)
    try:
        yield active.caller_id
    finally:
        if reserve_slot and concurrency_tracker is not None:
            concurrency_tracker.release(active.caller_id)


__all__ = [
    "Tracking",
    "admit",
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
