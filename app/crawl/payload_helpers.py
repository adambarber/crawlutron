"""Shared payload parsing helpers for crawl entry points.

Centralizes URL extraction and policy normalization used by both
``app.crawl.engine.crawl_request`` and the API routers that invoke it.
"""

from __future__ import annotations

from typing import Any

from app.api_models import POLICY_SCHEMA_VERSION
from app.constants import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_EXTRACTION,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_STRATEGIES,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    MAX_CONTENT_LENGTH_BYTES,
    MAX_RATE_LIMIT_RPS,
    SUPPORTED_EXTRACTIONS,
    SUPPORTED_STRATEGIES,
)
from app.crawl.credentials import authorization_value as _authorization_value
from app.crawl.credentials import origin_of
from app.settings import CrawlerSettings
from app.validation import HostPolicy, normalize_public_http_url

SUPPORTED_EXTRACTION_METHODS = set(SUPPORTED_EXTRACTIONS)


def _validated_url(url: str, *, host_policy: HostPolicy) -> str:
    try:
        return str(normalize_public_http_url(url, policy=host_policy))
    except ValueError as error:
        reason = str(error)
        if reason in {"unsupported_scheme", "invalid_url"}:
            reason = "invalid_url"
        raise ValueError(reason) from error


# Bounded default for max_pages. A missing or zero value must NOT mean
# "unlimited" (F4) - it falls back to this sane cap instead.
DEFAULT_MAX_PAGES = 100


def _payload_urls(payload: dict[str, Any], *, host_policy: HostPolicy) -> list[str]:
    """Extract the request URL batch, collapsing duplicates by page identity.

    ``normalize_public_http_url`` is the single source of truth for what counts
    as the "same page" (fragment / trailing-slash / tracking-param / param-order
    variants all canonicalize identically), so dedup here must key on that
    canonical form -- not the raw string. Identical pages crawl once; the first
    occurrence is kept, order-preserving. Both the engine and the render paths
    inherit the dedup here, and the batch-size check downstream counts unique
    pages. Validation happens before the key is built, so a refused target is
    never a distinct entry that can cross an acceptance boundary.
    """
    if "urls" in payload:
        urls = payload["urls"]
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("invalid_urls")
        seen: set[str] = set()
        unique: list[str] = []
        for url in urls:
            key = _validated_url(url, host_policy=host_policy)
            if key not in seen:
                seen.add(key)
                unique.append(url)
        return unique
    if "url" in payload and isinstance(payload["url"], str):
        _validated_url(payload["url"], host_policy=host_policy)
        return [payload["url"]]
    raise ValueError("missing_url")


def checked_payload_urls(
    payload: dict[str, Any],
    max_batch_size: int,
    *,
    host_policy: HostPolicy,
    urls: list[str] | None = None,
) -> list[str]:
    """Deduplicated URL batch, rejected if it exceeds the batch cap (L4).

    The single gate shared by the engine (sync) and the crawl endpoint (async
    accept path): the async handler must call this *before* returning 202 so
    an oversized batch is rejected at the boundary instead of being accepted
    and failing inside the detached background task, where the caller can no
    longer see why.

    ``urls`` carries an already-derived batch forward (tier-2 T3): the
    endpoint gate parses the payload once and hands the result to the engine,
    so a request never parses the same batch twice. A forwarded batch skips
    re-derivation but not the cap -- supplying urls cannot smuggle an
    oversized batch past the ceiling.
    """
    if urls is None:
        urls = _payload_urls(payload, host_policy=host_policy)
    else:
        for url in urls:
            if not isinstance(url, str):
                raise ValueError("invalid_url")
            _validated_url(url, host_policy=host_policy)
    if len(urls) > max_batch_size:
        raise ValueError("batch_size_exceeded")
    return urls


def _payload_policy(payload: dict[str, Any], settings: CrawlerSettings) -> dict[str, Any]:
    policy = payload.get("policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("invalid_policy")
    limits = policy.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("invalid_policy_limits")
    headers = policy.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("invalid_policy_headers")
    strategies = policy.get("strategies") or DEFAULT_STRATEGIES
    if (
        not isinstance(strategies, list)
        or not all(isinstance(strategy, str) for strategy in strategies)
        or not strategies
    ):
        raise ValueError("invalid_policy_strategies")
    if any(strategy not in SUPPORTED_STRATEGIES for strategy in strategies):
        raise ValueError("invalid_policy_strategy")
    extraction = policy.get("extraction") or DEFAULT_EXTRACTION
    if extraction not in SUPPORTED_EXTRACTION_METHODS:
        raise ValueError("invalid_policy_extraction")
    request_profile = policy.get("request_profile") or {}
    browser_profile = policy.get("browser_profile") or {}
    discovery = policy.get("discovery") or {}
    credentials = policy.get("credentials") or {}
    if (
        not isinstance(request_profile, dict)
        or not isinstance(browser_profile, dict)
        or not isinstance(discovery, dict)
        or not isinstance(credentials, dict)
    ):
        raise ValueError("invalid_policy_profile")
    try:
        max_depth = max(0, min(20, int(limits.get("max_depth", 0))))
        # F4: a missing/zero max_pages must be bounded, never "unlimited".
        # Any positive request is clamped to [1, 2000].
        requested_pages = int(limits.get("max_pages", 0))
        if requested_pages <= 0:
            max_pages = DEFAULT_MAX_PAGES
        else:
            max_pages = max(1, min(2000, requested_pages))
        respect_robots = bool(limits.get("respect_robots", True))
        max_rps = float(limits.get("max_rps", 0.0) or 0.0)
        if not 0.0 <= max_rps <= MAX_RATE_LIMIT_RPS:
            raise ValueError("invalid_policy_limits")
        max_content_length_bytes = int(limits.get("max_content_length_bytes", 0) or 0)
        if not 0 <= max_content_length_bytes <= MAX_CONTENT_LENGTH_BYTES:
            raise ValueError("invalid_policy_limits")
    except (TypeError, ValueError):
        raise ValueError("invalid_policy_limits") from None
    normalized_credentials: dict[str, dict[str, Any]] = {}
    for key, credential in credentials.items():
        # Job-replay boundary: keys arrive as stored text, so canonicalize
        # here (the wire layer rejected non-canonical keys; this is
        # idempotent on those). Shape errors are rejected, never dropped.
        if not isinstance(credential, dict):
            raise ValueError("invalid_policy_credentials")
        try:
            origin = origin_of(str(key))
            _authorization_value(credential)  # shape validation, value unused
        except ValueError:
            raise ValueError("invalid_policy_credentials") from None
        normalized_credentials[origin] = dict(credential)
    return {
        "schema_version": str(policy.get("schema_version", POLICY_SCHEMA_VERSION)),
        "strategies": strategies,
        "extraction": extraction,
        "scope_regex": policy.get("scope_regex"),
        "css_selector": policy.get("css_selector"),
        "xpath": policy.get("xpath"),
        "limits": {
            "timeout_seconds": int(limits.get("timeout_seconds", settings.default_timeout_seconds)),
            "max_retries": int(limits.get("max_retries", settings.default_max_retries)),
            "challenge_wait_seconds": int(
                limits.get("challenge_wait_seconds", DEFAULT_CHALLENGE_WAIT_SECONDS)
            ),
            "max_discovered_links": int(limits.get("max_discovered_links", DEFAULT_MAX_DISCOVERED_LINKS)),
            "respect_robots": respect_robots,
            "max_depth": max_depth,
            "max_pages": max_pages,
            "max_rps": max_rps,
            "max_content_length_bytes": max_content_length_bytes,
            "total_url_budget_seconds": max(
                0.1, min(900, float(limits.get("total_url_budget_seconds", DEFAULT_TOTAL_URL_BUDGET_SECONDS)))
            ),
        },
        "headers": {str(key): str(value) for key, value in headers.items()},
        "credentials": normalized_credentials,
        "request_profile": dict(request_profile),
        "browser_profile": dict(browser_profile),
        "discovery": {str(key): bool(value) for key, value in discovery.items()},
    }


__all__ = ["_payload_urls", "checked_payload_urls", "_payload_policy", "SUPPORTED_EXTRACTION_METHODS"]
