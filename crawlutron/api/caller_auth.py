"""Inbound caller identity: the key on the request, or anonymity.

A key arrives as ``X-API-Key`` or a Bearer token; no key is the anonymous
caller, a wrong key is a 401. Ownership elsewhere is an exact id match.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, TypedDict

from fastapi import Request

from crawlutron.admission.policy import DEFAULT_MAX_PAGES
from crawlutron.api import key_throttle
from crawlutron.api.exceptions import http_error
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.caller_key import CallerKey
from crawlutron.models.error_response import ErrorResponse
from crawlutron.models.request_profile_record import RequestProfileRecord
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
from crawlutron.transport.request_profile import cookie_namespace_for

if TYPE_CHECKING:
    from crawlutron.api.caller_concurrency import CallerConcurrencyTracker
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron.models.map_request import MapRequest
    from crawlutron.storage.job_store import JobStore

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


def operator_caller() -> CallerIdentity:
    """The operator trust plane's crawl identity: unbounded, caller_id 'admin'."""
    return CallerIdentity(caller_id="admin", authenticated=True, max_pages=0, max_concurrency=0)


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


def presented_key(request: Request) -> str | None:
    """The one credential-parsing funnel's public door: every guard that extracts
    a presented secret reads it here, never re-parses headers itself."""
    return _presented_key(request)


def record_operator_auth_failure(audit, *, actor: str = "anonymous") -> None:
    """One durable audit row per failed operator authentication (#697); the
    #696 counter stays the metric, this is the durable record -- never both
    wired at separate sites. No-op when no audit store is wired (tests)."""
    if audit is None:
        return
    audit.record_event(actor=actor, action="operator_auth_failure", target_key_id=None, outcome="refused")


def _raise_invalid_api_key() -> NoReturn:
    """Shared 401 for any failed /v1/keys credential."""
    from crawlutron.runtime.metrics import get_metrics

    get_metrics().record_auth_refusal()
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

    A caller key never enters, valid or not; no store lookup, so the door is
    not a key-validity oracle, and an unconfigured token seals it (fail closed).
    Failed guesses meter through the shared KeyVerifyThrottle's dedicated
    ``admin`` bucket (#659): guess-pacing, not CPU protection --
    compare_digest is cheap, so the meter's value is pacing the door away
    from unlimited online guessing. 5 misses/300s -> 60s fail-closed
    cooldown BEFORE any comparison work beyond the digest.
    """
    secret = _presented_key(request)
    admin_token = key_admin_token(request)
    if not secret or not admin_token:
        _raise_invalid_api_key()
    throttle = key_throttle.shared_throttle()
    throttle.check_admin()
    if not _constant_time_eq(secret, admin_token):
        throttle.record_admin_failure()
        record_operator_auth_failure(_audit_store(request))
        _raise_invalid_api_key()
    throttle.record_admin_success()
    return operator_caller()


async def _caller_from_secret(request: Request, secret: str) -> CallerIdentity:
    """Resolve a presented secret to an identity or raise 401.

    The meter runs BEFORE the PBKDF2: a credential in cooldown refuses without
    burning another derivation (#653). The meter is the single per-entry-point
    chokepoint, not per-door."""
    throttle = key_throttle.shared_throttle()
    throttle.check(secret)
    store = getattr(request.app.state, "caller_key_store", None)
    record = await store.averify_key(secret) if store is not None else None
    if record is None:
        throttle.record_failure(secret)
        _raise_invalid_api_key()
    throttle.record_success(secret)
    return identity_for_key(record)


async def get_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency: who this request crawls as.

    A presented credential that matches the configured operator token resolves
    to the operator identity (verify_admin_secret is fail-closed when unset);
    any other presented secret rides the keyed path unchanged."""
    secret = _presented_key(request)
    if not secret:
        return anonymous_caller()
    if verify_admin_secret(secret, request):
        key_throttle.shared_throttle().record_admin_success()
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


def refuse_anonymous_cookie_namespace(caller: CallerIdentity, profile: RequestProfileRecord) -> None:
    """422 when an anonymous caller declares a cookie namespace.

    Anonymous is ONE bounded shared identity (milestone M1), so a declared namespace is not a
    tenant boundary; the door refuses the declaration instead of downgrading.
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
    """The cookie namespace for one crawl: a keyed caller is pinned to its key id;
    an anonymous caller gets the shared default -- ANY declaration is refused.
    Doors return 422; this scrub covers bypass seams.
    """
    if caller.authenticated and caller.caller_id:
        return caller.caller_id
    return ""


@asynccontextmanager
async def bound_render_cookie_namespace(caller: CallerIdentity | None):
    """Bind the render door's cookie namespace for the caller.

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
    request takes the caller's bound, else stays unbounded; every door shares this.
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
) -> AsyncGenerator[str, None]:
    """Admit one request: resolve the page bound, the quotas, the cookie namespace.

    The caller's admit lock spans count->check->insert for async doors
    (``reserve_slot=False``) and count->check->reserve for sync doors.
    """
    active = caller if caller is not None else anonymous_caller()
    resolved = resolve_page_limit(active, requested_pages)
    if payload is not None and resolved <= 0:
        # /v1/crawl and /v1/map bind a surviving 0 to DEFAULT_MAX_PAGES
        # (stored == enforced); /v1/jobs passes no payload and keeps 0.
        resolved = DEFAULT_MAX_PAGES
    if payload is not None:
        payload.policy = _apply_payload_policy(payload, active, resolved)
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
    # One admit at a time per caller, so no two requests read the same
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


def _apply_payload_policy(payload: PolicyCarrier, active: CallerIdentity, resolved: int) -> CrawlPolicy:
    """Write the resolved page bound and the effective cookie namespace into a
    carrier's policy, scrubbing any namespace the door refused. /v1/crawl and
    /v1/map only; /v1/jobs passes no payload."""
    refuse_anonymous_cookie_namespace(active, payload.policy.request_profile)
    policy = payload.policy.model_copy(
        update={"limits": payload.policy.limits.model_copy(update={"max_pages": resolved})}
    )
    namespace = effective_cookie_namespace(policy.request_profile, active)
    if namespace != cookie_namespace_for(policy.request_profile):
        profile = dict(policy.request_profile)
        profile["cookie_namespace"] = namespace
        policy = policy.model_copy(update={"request_profile": profile})
    return policy


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
    "key_admin_token",
    "presented_key",
    "quota_denials",
    "record_operator_auth_failure",
    "refuse_anonymous_cookie_namespace",
    "resolve_page_limit",
    "verify_admin_secret",
]
