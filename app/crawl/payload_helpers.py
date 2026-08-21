"""Shared payload parsing helpers for crawl entry points.

Centralizes URL extraction and policy normalization used by both
``app.crawl.engine.crawl_request`` and ``app.service.run_crawl_job``.
"""

from __future__ import annotations

from app.api_models import POLICY_SCHEMA_VERSION
from app.constants import (
    DEFAULT_EXTRACTION,
    DEFAULT_STRATEGIES,
    SUPPORTED_EXTRACTIONS,
    SUPPORTED_STRATEGIES,
)
from app.settings import CrawlerSettings


SUPPORTED_EXTRACTION_METHODS = set(SUPPORTED_EXTRACTIONS)

# Bounded default for max_pages. A missing or zero value must NOT mean
# "unlimited" (F4) - it falls back to this sane cap instead.
DEFAULT_MAX_PAGES = 100


def _payload_urls(payload: dict[str, Any]) -> list[str]:
    if "urls" in payload:
        urls = payload["urls"]
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("invalid_urls")
        return urls
    if "url" in payload and isinstance(payload["url"], str):
        return [payload["url"]]
    raise ValueError("missing_url")


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
    if not isinstance(strategies, list) or not all(isinstance(strategy, str) for strategy in strategies) or not strategies:
        raise ValueError("invalid_policy_strategies")
    if any(strategy not in SUPPORTED_STRATEGIES for strategy in strategies):
        raise ValueError("invalid_policy_strategy")
    extraction = policy.get("extraction") or DEFAULT_EXTRACTION
    if extraction not in SUPPORTED_EXTRACTION_METHODS:
        raise ValueError("invalid_policy_extraction")
    request_profile = policy.get("request_profile") or {}
    browser_profile = policy.get("browser_profile") or {}
    discovery = policy.get("discovery") or {}
    if not isinstance(request_profile, dict) or not isinstance(browser_profile, dict) or not isinstance(discovery, dict):
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
    except (TypeError, ValueError):
        raise ValueError("invalid_policy_limits") from None
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
            "challenge_wait_seconds": int(limits.get("challenge_wait_seconds", 15)),
            "max_discovered_links": int(limits.get("max_discovered_links", 250)),
            "respect_robots": respect_robots,
            "max_depth": max_depth,
            "max_pages": max_pages,
        },
        "headers": {str(key): str(value) for key, value in headers.items()},
        "request_profile": dict(request_profile),
        "browser_profile": dict(browser_profile),
        "discovery": {str(key): bool(value) for key, value in discovery.items()},
    }


__all__ = ["_payload_urls", "_payload_policy", "SUPPORTED_EXTRACTION_METHODS"]
