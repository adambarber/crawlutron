"""The caller-auth CORE: quota law, admission, and the cookie-namespace binding.

FastAPI plumbing (Request dependencies, header parsing, key stores) stays in
api.caller_auth; everything here is caller law the MCP layer shares.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from crawlutron.admission.policy import DEFAULT_MAX_PAGES
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.caller_key import CallerKey
from crawlutron.service.errors import CookieNamespaceRefusal, QuotaRefusal
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
from crawlutron.transport.request_profile import cookie_namespace_for

if TYPE_CHECKING:
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron.models.map_request import MapRequest
    from crawlutron.models.request_profile_record import RequestProfileRecord
    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
    from crawlutron.storage.job_store import JobStore

    #: Every carrier of a mutable ``CrawlPolicy`` the admit helper writes the resolved
    #: page bound and cookie namespace into; crawl and map are the two today.
    PolicyCarrier = CrawlRequest | MapRequest


@runtime_checkable
class CallerQuotaAdmission(Protocol):
    """The admission PORT the doors consume: caller-quota law behind one interface.

    Doors resolve this from app wiring (ONE injection point), never per call site.
    """

    #: One declaration of the admit call-shape; the concrete class binds the law to it.
    admit: Callable[..., AbstractAsyncContextManager[str]]

    def resolve_page_limit(self, caller: CallerIdentity, requested_max_pages: int) -> int: ...

    def effective_cookie_namespace(self, profile: Mapping[str, object], caller: CallerIdentity) -> str: ...

    def refuse_anonymous_cookie_namespace(
        self, caller: CallerIdentity, profile: RequestProfileRecord
    ) -> None: ...

    def anonymous_caller(self) -> CallerIdentity: ...

    def bound_render_cookie_namespace(
        self, caller: CallerIdentity | None
    ) -> AbstractAsyncContextManager[None]: ...


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


def refuse_anonymous_cookie_namespace(caller: CallerIdentity, profile: RequestProfileRecord) -> None:
    """422 when an anonymous caller declares a cookie namespace.

    Anonymous is ONE bounded shared identity (milestone M1), so a declared namespace is not a
    tenant boundary; the door refuses the declaration instead of downgrading.
    """
    if (caller.authenticated and caller.caller_id) or not cookie_namespace_for(profile):
        return
    raise CookieNamespaceRefusal(
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
        raise QuotaRefusal(
            f"Caller page quota is {caller.max_pages}, requested {requested_max_pages}",
        )


def check_concurrency_quota(caller: CallerIdentity, active_count: int) -> None:
    """Refuse when the caller's live jobs already fill its concurrency budget."""
    if caller.max_concurrency > 0 and active_count >= caller.max_concurrency:
        raise QuotaRefusal(
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


class DefaultCallerQuotaAdmission:
    """The concrete port: the module's admission law behind the interface."""

    def __init__(self) -> None:
        self.admit = admit

    def resolve_page_limit(self, caller: CallerIdentity, requested_max_pages: int) -> int:
        return resolve_page_limit(caller, requested_max_pages)

    def effective_cookie_namespace(self, profile: Mapping[str, object], caller: CallerIdentity) -> str:
        return effective_cookie_namespace(profile, caller)

    def refuse_anonymous_cookie_namespace(
        self, caller: CallerIdentity, profile: RequestProfileRecord
    ) -> None:
        refuse_anonymous_cookie_namespace(caller, profile)

    def anonymous_caller(self) -> CallerIdentity:
        return anonymous_caller()

    def bound_render_cookie_namespace(
        self, caller: CallerIdentity | None
    ) -> AbstractAsyncContextManager[None]:
        return bound_render_cookie_namespace(caller)


#: The default instance the wiring stashes; substitution happens at the wiring site.
#: Named cast for basedpyright CM variance; conformance is asserted by test_caller_quota_port.py.
CALLER_QUOTA_ADMISSION = cast("CallerQuotaAdmission", DefaultCallerQuotaAdmission())

__all__ = [
    "CALLER_QUOTA_ADMISSION",
    "CallerQuotaAdmission",
    "DefaultCallerQuotaAdmission",
    "Tracking",
    "admit",
    "anonymous_caller",
    "bound_render_cookie_namespace",
    "check_concurrency_quota",
    "check_page_quota",
    "effective_cookie_namespace",
    "identity_for_key",
    "operator_caller",
    "refuse_anonymous_cookie_namespace",
    "resolve_page_limit",
]
