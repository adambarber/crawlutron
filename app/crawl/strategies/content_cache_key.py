"""Owns content-cache keys and RAM-scaled content limits.

This module owns the stable cache identity and policy limit resolution for HTTP responses.
It must not fetch, decode, extract, or construct crawl results.
"""

from __future__ import annotations

import json

from app.api_models import CrawlPolicyLimits
from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from app.crawl.content_cache import ContentCache
from app.validation import get_env_int


def _content_cache_key(
    normalized_url: str,
    strategy: str,
    base_headers: dict[str, str],
    extraction: str | None,
    css_selector: str | None,
    xpath: str | None,
) -> str:
    """Stable content-cache key mirroring what http.py stores under.

    Kept as a shared helper so engine.py can reconstruct the exact key a future
    fetch of ``normalized_url`` would use (for sitemap lastmod freshness checks,
    #3) without duplicating the key composition.

    ``base_headers`` is serialized WHOLE, and that deliberately includes the
    per-request ``Cookie`` header set by ``_headers_for_strategy`` (P2-G). The
    comment at the call site documents the intentional EXCLUSION of the
    conditional-revalidation headers; ``Cookie`` is the opposite case -- an
    intentional INCLUSION, and a safety property rather than an optimization:
    the cookie namespace is what separates one tenant's authenticated view of a
    URL from another's, so a key that ignored ``Cookie`` could serve tenant A's
    private page to tenant B straight out of tenant A's cache slot (cross-tenant
    cache poisoning). The cookie header participates in the key exactly so that
    differing credentials can never share a cache entry.
    """
    return ContentCache.make_key(
        "http",
        normalized_url,
        strategy,
        json.dumps(sorted(base_headers.items()), separators=(",", ":")),
        extraction,
        css_selector,
        xpath,
    )


# Adversarial perf #2.1: bound the RAM consumed by in-flight page bodies. At high
# global_concurrency the per-page cap shrinks so the whole active set cannot
# exhaust the budget. Operators with known RAM headroom can override the budget.
_RAM_SAFETY_BYTES = get_env_int("BROWSER_CRAWLER_CONTENT_RAM_BUDGET_BYTES", 1 * 1024 * 1024 * 1024, lo=1)
_MIN_PER_PAGE_BYTES = 256 * 1024  # never shrink the cap below 256 KiB


def _default_global_concurrency() -> int:
    from app.settings import CrawlerSettings

    try:
        return max(1, int(CrawlerSettings.from_mapping().global_concurrency))
    except Exception:  # pragma: no cover - settings should always parse
        return 4


def _effective_max_content_length(limits: CrawlPolicyLimits, global_concurrency: int | None = None) -> int:
    """Per-page body cap, scaled down as concurrency rises (adversarial perf #2.1).

    An explicit ``max_content_length_bytes`` in the policy always wins. Otherwise
    the cap is ``min(DEFAULT, RAM_BUDGET / concurrency)`` so the in-flight page
    set stays within budget even when hundreds of pages run concurrently. At the
    default concurrency (4) this resolves to the unchanged 10 MiB default.
    """
    # A model-bound int, so no re-validation: 0 and None both mean "no explicit
    # cap", and the model's le is the same ceiling this used to clamp to.
    if limits.max_content_length_bytes:
        return limits.max_content_length_bytes
    conc = global_concurrency or _default_global_concurrency()
    scaled = min(
        DEFAULT_MAX_CONTENT_LENGTH_BYTES,
        max(_MIN_PER_PAGE_BYTES, _RAM_SAFETY_BYTES // max(conc, 1)),
    )
    return int(scaled)
