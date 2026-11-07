"""``POST /v1/admit`` — the pre-flight admission contract.

The base check (``check_robots=false``) is pure: no I/O, no quota, no store
writes; the only I/O path is the opt-in robots check through the shared loader.
"""

from __future__ import annotations

import logging

from crawlutron.models.admit_request import AdmitRequest
from crawlutron.models.admit_response import AdmitChecks, AdmitReason, AdmitResponse
from crawlutron.models.host_policy import HostPolicy, host_egress_reason, host_policy_reason
from crawlutron.models.normalized_url import NormalizedUrl, admit_with_reason
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.robots import load_robots_rules, robots_allows

logger = logging.getLogger(__name__)

#: The per-check table's keys, in the order the checks run.
_CHECK_KEYS = ("url", "host_policy", "egress", "robots")

#: The constructor's structural refusals, which name the url check itself.
_URL_CHECK_REASONS = frozenset({"invalid_url", "unsupported_scheme"})

#: The reason vocabulary, as a table: an unknown reason is a KeyError, not a
#: silently widened value -- the breakdown cannot name anything un-declared.
_REASON_TABLE: dict[str, AdmitReason] = {
    "invalid_url": "invalid_url",
    "unsupported_scheme": "unsupported_scheme",
    "host_denied": "host_denied",
    "host_not_allowed": "host_not_allowed",
    "private_network_target": "private_network_target",
    "robots_disallowed": "robots_disallowed",
}


def _reason(reason: str) -> AdmitReason:
    return _REASON_TABLE[reason]


def _blank_checks() -> AdmitChecks:
    return {key: None for key in _CHECK_KEYS}


def _slot_for(reason: str | None) -> str:
    """Which check a constructor refusal belongs to: structural ones are the url
    check; the host ones are the constructor's own policy/egress call order."""
    if reason in _URL_CHECK_REASONS:
        return "url"
    if reason == "host_denied" or reason == "host_not_allowed":
        return "host_policy"
    return "egress"


def _refuse(checks: AdmitChecks, *, canonical: NormalizedUrl | None) -> AdmitResponse:
    """The breakdown IS the verdict: the first refused check names the outcome."""
    reason: AdmitReason = "invalid_url"
    for value in checks.values():
        if value is not None:
            reason = value
            break
    return AdmitResponse(admitted=False, classification=reason, canonical_url=canonical, checks=checks)


def _admit_pure(normalized: NormalizedUrl, *, policy: HostPolicy, checks: AdmitChecks) -> AdmitResponse:
    """The host-policy and egress arms, in the constructor's own order."""
    policy_reason = host_policy_reason(normalized.host, policy=policy)
    checks["host_policy"] = None if policy_reason is None else _reason(policy_reason)
    egress_reason = host_egress_reason(normalized.host)
    checks["egress"] = None if egress_reason is None else _reason(egress_reason)
    if policy_reason is None and checks["egress"] is None:
        return AdmitResponse(admitted=True, classification=None, canonical_url=normalized, checks=checks)
    return _refuse(checks, canonical=normalized)


async def admit_url(payload: AdmitRequest, *, settings) -> AdmitResponse:
    """Judge one URL and surface the constructor's own reason as data.

    Pure when ``check_robots`` is false; the robots arm is the sole I/O path and
    rides the process-wide TTL cache every robots consumer shares.
    """
    policy = HostPolicy.from_settings(settings)
    normalized, raw_reason = admit_with_reason(payload.url, policy=policy)
    checks = _blank_checks()
    if normalized is None:
        reason = raw_reason or "invalid_url"
        checks[_slot_for(reason)] = _reason(reason)
        return _refuse(checks, canonical=None)
    verdict = _admit_pure(normalized, policy=policy, checks=checks)
    if not verdict.admitted or not payload.check_robots:
        return verdict
    rules = await load_robots_rules(
        normalized, default_http_fetcher, int(settings.default_timeout_seconds), policy=policy
    )
    if not robots_allows(normalized, rules.rules):
        checks["robots"] = _reason("robots_disallowed")
        return _refuse(checks, canonical=normalized)
    return AdmitResponse(admitted=True, classification=None, canonical_url=normalized, checks=checks)


__all__ = ["admit_url"]
