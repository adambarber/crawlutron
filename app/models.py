from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, is_dataclass, fields
from typing import Any, Literal

from app.constants import (
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)


ResultStatus = Literal["success", "blocked", "disallowed", "retry_exhausted", "failed", "skipped"]
Classification = Literal[
    "rendered",
    "captcha_detected",
    "bot_challenge_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
    "cookie_consent_required",
    "javascript_required",
    "unsupported_browser",
    "geolocation_blocked",
    "waf_challenge_detected",
    "not_found",
    "site_error",
    "gone",
    "binary_no_text",
    "robots_disallowed",
    "navigation_timeout",
    "browser_crash",
    "network_error",
    "connection_error",
    "http_status_error",
    "value_error",
    "tls_error",
    "dns_error",
    "invalid_url",
    "unsupported_scheme",
    "private_network_target",
    "unknown_error",
    "not_modified",
]

CrawlStrategyName = Literal[
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
]


@dataclass(frozen=True)
class RenderOptions:
    timeout_seconds: int = 30
    wait_until: str = "network_idle_or_timeout"
    max_retries: int = 2
    respect_robots: bool = True
    include_html: bool = True
    include_text: bool = True
    browser_profile: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrawlJob:
    index: int
    url: str
    domain: str
    options: RenderOptions


@dataclass(frozen=True)
class CrawlResult:
    url: str
    status: ResultStatus
    classification: Classification
    final_url: str | None
    html: str | None
    text: str | None
    attempts: int
    elapsed_ms: int
    domain: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class HttpFetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    # True when a Content-Encoding indicated compression but decompression
    # failed silently (garbage bytes delivered as-is). Downstream consumers
    # downgrade quality and flag diagnostics instead of trusting the text.
    decompression_failed: bool = False


@dataclass(frozen=True)
class ClassificationEvidence:
    classification: str
    signature: str
    source: str
    context: str


@dataclass(frozen=True)
class CrawlStrategyAttempt:
    strategy: CrawlStrategyName
    status: ResultStatus
    classification: Classification
    http_status: int | None
    content_type: str | None
    elapsed_ms: int
    error: str | None = None


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    normalized_url: str
    text: str
    rel: list[str]
    source_url: str
    host: str
    is_same_host: bool


@dataclass(frozen=True)
class EngineCrawlResult:
    original_url: str
    normalized_url: str | None
    final_url: str | None
    status: ResultStatus
    classification: Classification
    selected_strategy: CrawlStrategyName | None
    attempted_strategies: list[CrawlStrategyAttempt]
    http_status: int | None
    headers: dict[str, str]
    content_type: str | None
    html: str | None
    text: str | None
    extraction_method: str | None
    quality_score: float
    classification_evidence: list[ClassificationEvidence] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    discovered_link_count: int = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLink] = field(default_factory=list)
    error: str | None = None
    markdown: str | None = None
    next_page_url: str | None = None
    # Origin validators for the delivered representation ("etag"/"last_modified").
    # Empty when the origin supplied none; surfaced so callers can revalidate
    # conditionally on later requests (freshness monitoring).
    validators: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared callable aliases (single source of truth for worker/fetcher types).
# `CrawlWorker` is the per-URL render/strategy worker used by the engine and
# service layers; `SchedulerWorker` (defined in app.scheduler) is the worker the
# domain scheduler drives, which returns the internal engine result.
# ---------------------------------------------------------------------------
CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult] | CrawlResult]
HttpFetcher = Callable[[str, dict[str, str], int], Any]


def to_dict(result: Any) -> dict[str, Any]:
    """Return a JSON-serializable dict for any frozen-dataclass crawl result.

    Unlike ``dataclasses.asdict``, this does NOT ``deepcopy`` leaf values. The
    heavy fields (``html``/``text`` strings, ``metadata``/``diagnostics``
    dicts) are shared by reference, and only nested dataclasses (e.g.
    ``DiscoveredLink``) are converted to dicts -- so the output stays
    JSON-serializable without paying asdict's full recursive deep-copy cost on
    every field of every result in a large batch (#2).
    """
    return _to_jsonable(result)


def _to_jsonable(value: Any) -> Any:
    return _to_jsonable_impl(value, set())


def _to_jsonable_impl(value: Any, seen: set[int]) -> Any:
    # Guard against cyclic dataclass graphs (e.g. a DiscoveredLink whose
    # parent/children point back to an ancestor). Without this, a cycle would
    # recurse forever and raise RecursionError during result serialization (B2).
    # Only mutable containers can participate in a reference cycle, so we track
    # those by id; primitives (str/int/bytes) are returned as-is and must never
    # be flagged (small strings may be interned and share an id across the tree).
    if is_dataclass(value) and not isinstance(value, type):
        oid = id(value)
        if oid in seen:
            return None
        seen = seen | {oid}
        return {f.name: _to_jsonable_impl(getattr(value, f.name), seen) for f in fields(value)}
    if isinstance(value, dict):
        oid = id(value)
        if oid in seen:
            return None
        seen = seen | {oid}
        return {k: _to_jsonable_impl(v, seen) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        oid = id(value)
        if oid in seen:
            return None
        seen = seen | {oid}
        return [_to_jsonable_impl(v, seen) for v in value]
    return value
