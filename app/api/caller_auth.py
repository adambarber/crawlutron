"""Inbound caller identity: the key on the request, or anonymity.

A key arrives as ``X-API-Key`` or a Bearer token; no key is the anonymous
caller, a wrong key is a 401. Ownership elsewhere is an exact id match.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, TypedDict

from fastapi import Request

from app.api.exceptions import http_error
from app.config.settings import CrawlerSettings
from app.models.caller_identity import CallerIdentity
from app.models.caller_key import CallerKey
from app.models.error_response import ErrorResponse
from app.models.request_profile_record import RequestProfileRecord
from app.transport.cookie_jar import COOKIE_NAMESPACE
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


def _presented_key(request: Request) -> str | None:
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


def _key_admin_token(request: Request) -> str:
    """The configured operator token, or '' when unset (#142: via the settings home)."""
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
    return CrawlerSettings.from_mapping().key_admin_token


def _constant_time_eq(secret: str, token: str) -> bool:
    """Constant-time compare that tolerates non-ASCII str.

    ``secrets.compare_digest`` raises TypeError on non-ASCII str, which would
    500 a keys-door denial; encoding both sides to bytes fixes it (#199).
    """
    return (
        bool(secret) and bool(token) and secrets.compare_digest(secret.encode("utf-8"), token.encode("utf-8"))
    )


async def get_keys_caller(request: Request) -> CallerIdentity:
    """FastAPI dependency: the operator token is the only /v1/keys caller (#436).

    A caller key never enters, valid or not; no store lookup, so the door is
    not a key-validity oracle, and an unconfigured token seals it (fail closed).
    """
    secret = _presented_key(request)
    admin_token = _key_admin_token(request)
    if not secret or not admin_token or not _constant_time_eq(secret, admin_token):
        _raise_invalid_api_key()
    return CallerIdentity(
        caller_id="admin",
        authenticated=True,
        max_pages=0,
        max_concurrency=0,
    )


async def _caller_from_secret(request: Request, secret: str) -> CallerIdentity:
    """Resolve a presented secret to an identity or raise 401."""
    store = getattr(request.app.state, "caller_key_store", None)
    record = await store.averify_key(secret) if store is not None else None
    if record is None:
        _raise_invalid_api_key()
    return identity_for_key(record)


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency: who this request crawls as."""
    secret = _presented_key(request)
    if not secret:
        return anonymous_caller()
    return await _caller_from_secret(request, secret)


def is_operator(request: Request) -> bool:
    """Whether the presented credential is the configured operator token.

    Soft gate, not a 401: ``/metrics`` keeps its base exposition loopback-accessible
    and only per-caller detail needs the token (constant-time compare).
    """
    secret = _presented_key(request)
    if not secret:
        return False
    admin_token = _key_admin_token(request)
    return bool(admin_token) and _constant_time_eq(secret, admin_token)


def refuse_anonymous_cookie_namespace(caller: CallerIdentity, profile: RequestProfileRecord) -> None:
    """422 when an anonymous caller declares a cookie namespace (#559).

    Anonymous is ONE bounded shared identity (M1 #206), so a declared namespace is not a
    tenant boundary; the door refuses the declaration instead of downgrading (#537).
    """
    if (caller.authenticated and caller.caller_id) or not cookie_namespace_for(profile):
        return
    raise http_error(
        422,
        "anonymous_cookie_namespace",
        "Anonymous callers may not declare request_profile.cookie_namespace; "
        "anonymous crawling shares one bounded cookie jar. Present an API key to "
        "crawl under your key id's namespace.",
    )


def effective_cookie_namespace(profile: Mapping[str, object], caller: CallerIdentity) -> str:
    """The cookie namespace for one crawl: a keyed caller is pinned to its key id (#226);
    an anonymous caller gets the shared default -- ANY declaration is refused (#559,
    extending #537 to every namespace). Doors return 422; this scrub covers bypass seams.
    """
    if caller.authenticated and caller.caller_id:
        return caller.caller_id
    return ""


@asynccontextmanager
async def bound_render_cookie_namespace(caller: CallerIdentity | None):
    """Bind the render door's cookie namespace for the caller (#483).

    /v1/render has no policy carrier, so a keyed caller's browser harvest is
    pinned to the key id here; an anonymous render keeps the shared default.
    """
    namespace = caller.caller_id if caller is not None and caller.authenticated and caller.caller_id else ""
    token = COOKIE_NAMESPACE.set(namespace)
    try:
        yield
    finally:
        COOKIE_NAMESPACE.reset(token)


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

    #464: the caller's admit lock spans count->check->insert for async doors
    (``reserve_slot=False``) and count->check->reserve for sync doors.
    """
    active = caller if caller is not None else anonymous_caller()
    resolved = resolve_page_limit(active, requested_pages)
    if payload is not None:
        # #559: an anonymous namespace declaration is refused at the door (422),
        # not silently downgraded; the declared profile is the caller's claim.
        refuse_anonymous_cookie_namespace(active, payload.policy.request_profile)
        policy = payload.policy.model_copy(
            update={"limits": payload.policy.limits.model_copy(update={"max_pages": resolved})}
        )
        namespace = effective_cookie_namespace(policy.request_profile, active)
        # #537/#559: scrub a refused declaration so the engine cannot re-derive it.
        if namespace != cookie_namespace_for(policy.request_profile):
            profile = dict(policy.request_profile)
            profile["cookie_namespace"] = namespace
            policy = policy.model_copy(update={"request_profile": profile})
        payload.policy = policy
    check_page_quota(active, resolved)
    job_store = tracking.job_store
    concurrency_tracker = tracking.concurrency_tracker
    if concurrency_tracker is None:
        active_count = (
            await job_store.aactive_count_for_caller(active.caller_id) if job_store is not None else 0
        )
        check_concurrency_quota(active, active_count)
        yield active.caller_id
        return
    # #464: one admit at a time per caller, so no two requests read the same
    # count before either has inserted its row or reserved its slot.
    async with concurrency_tracker.slot_lock(active.caller_id):
        active_count = (
            await job_store.aactive_count_for_caller(active.caller_id) if job_store is not None else 0
        )
        active_count += concurrency_tracker.sync_in_flight(active.caller_id)
        check_concurrency_quota(active, active_count)
        if not reserve_slot:
            # The row insert runs inside the lock; once it lands, the durable
            # row is the reservation the next admit counts.
            yield active.caller_id
            return
        concurrency_tracker.reserve(active.caller_id)
    try:
        yield active.caller_id
    finally:
        concurrency_tracker.release(active.caller_id)


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
    "quota_denials",
    "refuse_anonymous_cookie_namespace",
    "resolve_page_limit",
]
