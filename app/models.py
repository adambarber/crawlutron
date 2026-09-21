from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, get_args

from app.constants import (
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
    ResultStatus,
)

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
    # The per-URL budget overran: the host answered but the whole chain
    # exceeded total_url_budget_seconds. A CrawlClassification member (see
    # app/constants.py) so it survives the strict enum at response shaping.
    "url_budget_exceeded",
    # Fetch-failure codes the HTTP strategy's error classifier emits; they are
    # CrawlClassification members (see app.api_models) so they survive the
    # strict enum field at response shaping (H3).
    "payload_too_large",
    "too_many_redirects",
    "http_error",
    "value_error",
    "tls_error",
    "dns_error",
    "invalid_url",
    "unsupported_scheme",
    "private_network_target",
    "unknown_error",
    "not_modified",
    # A browser renderer slot was unavailable within the acquire bound, so the
    # URL was not attempted; the frontier re-queues it (at-least-once). A
    # CrawlClassification member so it survives the strict enum at response
    # shaping.
    "browser_pool_exhausted",
    # A domain's circuit breaker was open, so the URL was not attempted: a
    # scheduler capacity decision, not a page outcome. A CrawlClassification
    # member for the same reason -- a non-member code would reach the strict
    # enum and be downgraded to site_error, hiding the skip reason.
    "circuit_breaker_open",
]

# The literal members as a runtime set. ``Classification`` crosses a string
# boundary in a few places (an exception message carrying a taxonomy code, a
# cached dict value), and narrowing one back to the Literal needs a membership
# check against exactly these values -- derived from the alias so the set and
# the enum cannot drift.
CLASSIFICATION_MEMBERS: frozenset[str] = frozenset(get_args(Classification))


def _is_classification(value: str) -> bool:
    """Report whether ``value`` names a real :data:`Classification` member.

    The taxonomy code an exception carries is a ``str`` at the point it is read
    off ``str(exc)``; this membership check is what lets ``as_classification``
    hand back the Literal instead of widening every consumer to ``str``.
    """
    return value in CLASSIFICATION_MEMBERS


_CLASSIFICATION_TABLE: dict[str, Classification] = {name: name for name in get_args(Classification)}


def as_classification(value: str, *, default: Classification = "unknown_error") -> Classification:
    """Return ``value`` as a :data:`Classification`, or ``default`` if it is not one.

    Callers that pull a code off a string boundary (an exception message, a
    cached dict value) route through here rather than asserting, so an
    untriaged code degrades to the documented default instead of widening the
    classification type at every consumer. The table is built from the alias's
    own members so the mapping and the enum cannot drift; its values are the
    Literal members, which is what makes the return provably narrow.
    """
    return _CLASSIFICATION_TABLE.get(value, default)


CrawlStrategyName = Literal[
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
]


@dataclass(frozen=True)
class RenderOptions:
    timeout_seconds: int = 30
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
class CrawlStrategyAttempt:
    # Optional, not bare CrawlStrategyName: the worker-exception and
    # budget-timeout envelopes are built after a failure that preceded any
    # strategy attempt, and "no strategy was attempted" is not a strategy. The
    # honest value there is None, and the wire model serializes it as null.
    strategy: CrawlStrategyName | None
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


@dataclass
class CrawlTask:
    """A single URL scheduled for crawling within a crawl_request.

    Carries the per-job bookkeeping the engine/recursive dispatcher needs
    (submission index for result ordering, original + normalized URL, and the
    registrable domain used for per-domain scheduling/robots scoping).
    """

    index: int
    original_url: str
    normalized_url: str
    domain: str


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
    classification_evidence: list[dict[str, Any]] = field(default_factory=list)
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
# The union is honest: a worker may be a coroutine function (the browser
# strategies and render-path instances) or a plain synchronous function (some
# HTTP/test workers). Callers MUST dispatch on the callable before invoking
# (see ``_call_worker``) rather than awaiting the result blindly -- awaiting a
# non-awaitable was the original double-invocation bug. The sync/async members
# are broken out so the dispatch can narrow without a cast.
SyncCrawlWorker = Callable[[CrawlJob], CrawlResult]
AsyncCrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult]]
CrawlWorker = SyncCrawlWorker | AsyncCrawlWorker
# Four positional parameters: (url, headers, timeout_seconds, max_content_length).
# Every concrete fetcher accepts the content-length cap
# (_default_http_fetcher / _default_cffi_fetcher) and the retry wrapper passes
# it positionally, so the alias must describe four parameters -- a caller typed
# against it must be able to invoke the real fetcher it is handed (P2-A).
HttpFetcher = Callable[[str, dict[str, str], int, int], Any]


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


class BrowserPoolExhaustedError(Exception):
    """No renderer became available within the acquire timeout.

    Lives in ``app.models`` (not ``browser_pool``) because the shared error
    classifier in ``app.crawl.result_helpers`` must isinstance-check it
    without importing a strategy module: browser_pool imports browser, and
    browser imports result_helpers, so defining it there would create an
    import cycle. ``browser_pool`` re-exports it for its existing importers.
    """
