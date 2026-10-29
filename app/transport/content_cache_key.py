"""Owns content-cache keys and RAM-scaled content limits.

This module owns the stable cache identity and policy limit resolution for HTTP responses.
It must not fetch, decode, extract, or construct crawl results.
"""

from __future__ import annotations

import json

from app.config.env import get_env_int
from app.models.crawl_policy_limits import CrawlPolicyLimits
from app.models.normalized_url import NormalizedUrl
from app.runtime.pressure_sizing import worker_count
from app.transport.content_cache import ContentCache
from app.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES


def build_content_cache_key(
    normalized_url: NormalizedUrl,
    strategy: str,
    base_headers: dict[str, str],
    extraction: str | None,
    css_selector: str | None,
    xpath: str | None,
    cookie_namespace: str = "",
) -> str:
    """Stable content-cache key mirroring what http.py stores under -- a shared
    helper reconstructing a future fetch's exact key (the cookie
    namespace partitions callers whose header sets collide).
    """
    # pre-flip key was built from, so every entry already on disk stays addressable after
    # the object flip.
    return ContentCache.make_key(
        "http",
        normalized_url.raw,
        strategy,
        cookie_namespace,
        json.dumps(sorted(base_headers.items()), separators=(",", ":")),
        extraction,
        css_selector,
        xpath,
    )


# Bound the RAM consumed by in-flight page bodies, a container quantity divided by the
# worker count where it is read.
_MIN_PER_PAGE_BYTES = 256 * 1024  # never shrink the cap below 256 KiB


def content_ram_budget_bytes() -> int:
    """The in-flight page-body RAM budget, read per call: the knob stays live."""
    return get_env_int("CRAWLUTRON_CONTENT_RAM_BUDGET_BYTES", 1 * 1024 * 1024 * 1024, lo=1)


def _default_global_concurrency() -> int:
    from app.config.settings import CrawlerSettings

    try:
        return max(1, int(CrawlerSettings.from_mapping().global_concurrency))
    except Exception:  # pragma: no cover - settings should always parse
        return 4


def effective_max_content_length(limits: CrawlPolicyLimits, global_concurrency: int | None = None) -> int:
    """Per-page body cap, scaled down as concurrency rises: an explicit
    ``max_content_length_bytes`` always wins, else ``min(DEFAULT, RAM/(concurrency
    x workers))`` so the in-flight set stays in budget.
    """
    # A model-bound int, so no re-validation: 0 and None both mean "no explicit
    # cap", and the model's le is the same ceiling this used to clamp to.
    if limits.max_content_length_bytes:
        return limits.max_content_length_bytes
    conc = global_concurrency or _default_global_concurrency()
    scaled = min(
        DEFAULT_MAX_CONTENT_LENGTH_BYTES,
        max(_MIN_PER_PAGE_BYTES, content_ram_budget_bytes() // (max(conc, 1) * worker_count())),
    )
    return int(scaled)
