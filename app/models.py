from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING, Any, Literal, NewType, get_args

from app.constants import ResultStatus

if TYPE_CHECKING:
    from app.validation import HostPolicy

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


# The stable machine-readable failure code carried alongside ``classification``.
# Documented at app/api/render.py:202 as a stable code callers can key on, so it
# is a declared taxonomy rather than a free-form str: a code that is not listed
# here cannot reach a result, and the membership sweep enforces it for free.
# - page-outcome codes that are also Classification members: the builders route
#   ``error=classification`` for these (result_helpers), so the two fields agree
#   and the code is a stable key for callers.
# - pipeline conditions with no page-classification analogue
#   (``browser_worker_unavailable`` is a capacity decision, ``unsupported_strategy``
#   a config one): they are not page outcomes and forcing them into Classification
#   would distort that taxonomy.
CrawlError = Literal[
    # Every Classification member: the builders route ``error=classification``
    # (result_helpers), so the two fields agree and the code is a stable key for
    # callers. Listed explicitly because mypy cannot evaluate ``*get_args()`` in
    # a Literal alias; test_crawl_error_members_derive_from_classification pins
    # this list to Classification's args so it cannot drift from the source.
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
    "url_budget_exceeded",
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
    "browser_pool_exhausted",
    "circuit_breaker_open",
    # Pipeline conditions with no page-classification analogue: a capacity
    # decision and a config one. They are not page outcomes, so forcing them into
    # Classification would distort that taxonomy.
    "robots_txt_disallowed",
    "browser_worker_unavailable",
    "unsupported_strategy",
    "retries_exhausted",
]

CrawlStrategyName = Literal[
    "HTTP_DECLARED_CRAWLER",
    "HTTP_CFFI_IMPERSONATE",
    "BROWSER_HEADLESS",
    "BROWSER_HEADED_WARMED",
]


# Fields whose declared type is a Literal: a value that is not a member is a
# shape lie, so the dataclasses validate them at construction. Kept here next to
# the Literal definitions because the check and the types must move together.
_LITERAL_FIELDS = {
    "status": set(get_args(ResultStatus)),
    "classification": set(get_args(Classification)),
    "selected_strategy": set(get_args(CrawlStrategyName)),
    "strategy": set(get_args(CrawlStrategyName)),
    "error": set(get_args(CrawlError)),
}


def _check_literal_fields(obj: Any) -> None:
    """Reject a non-member value on any Literal field of a result dataclass.

    This is the construction-time chokepoint: ``result_helpers`` builds most
    results, but a dataclass built anywhere else (engine.py, strategies, tests)
    previously escaped the only runtime guard. Checking in ``__post_init__``
    closes the class by construction -- no future direct build can carry an
    out-of-taxonomy value -- for the cost of one set lookup per field.
    Namespace/provenance (a member *value* arriving from the wrong constant) is
    not covered here and stays with the AST detector.
    """
    for name, allowed in _LITERAL_FIELDS.items():
        if name not in obj.__dict__:
            continue
        value = obj.__dict__[name]
        if value is not None and value not in allowed:
            raise ValueError(
                f"{type(obj).__name__}.{name}={value!r} is not a member of its "
                f"declared Literal ({len(allowed)} allowed)"
            )


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
    host_policy: HostPolicy


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
    error: CrawlError | None = None
    # The browser mode that actually served this render (BROWSER_HEADLESS or
    # BROWSER_HEADED_WARMED), or None when no browser strategy produced it. The
    # render endpoint surfaces this on the wire as ``selected_strategy`` so the
    # caller can tell a warmed render from a plain headless one; None rather
    # than a hard-coded member keeps "we did not render this" distinguishable
    # from "we rendered it headless".
    selected_strategy: CrawlStrategyName | None = None

    def __post_init__(self) -> None:
        _check_literal_fields(self)



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
    error: CrawlError | None = None

    def __post_init__(self) -> None:
        _check_literal_fields(self)


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    # M6: the frontier/visited-eligible form; producers build it from
    # ``normalize_public_http_url`` output (``frontier_link_url`` is the only
    # read allowed for queueing -- the raw ``url`` href stays unqueue-able).
    normalized_url: NormalizedUrl
    text: str
    rel: list[str]
    source_url: str
    host: str
    is_same_host: bool


# M6: the canonical form produced by ``app.validation.normalize_public_http_url``
# (the single source of truth for "what a normalized URL is"). A NewType over
# str: erased at runtime -- spill JSON, cache keys, and the wire keep plain
# strings -- but mypy rejects putting a raw request/sitemap/crawl string into
# a normalized-URL position unless it has crossed the normalizer.
NormalizedUrl = NewType("NormalizedUrl", str)


@dataclass
class CrawlTask:
    """A single URL scheduled for crawling within a crawl_request.

    Carries the per-job bookkeeping the engine/recursive dispatcher needs
    (submission index for result ordering, original + normalized URL, and the
    registrable domain used for per-domain scheduling/robots scoping).
    """

    index: int
    original_url: str
    normalized_url: NormalizedUrl
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
    error: CrawlError | None = None
    markdown: str | None = None
    next_page_url: str | None = None
    # Origin validators for the delivered representation ("etag"/"last_modified").
    # Empty when the origin supplied none; surfaced so callers can revalidate
    # conditionally on later requests (freshness monitoring).
    validators: dict[str, str] = field(default_factory=dict)
    # Page metadata (title/og/twitter/JSON-LD) computed once from the engine's
    # shared discovery tree (H2). None = never computed here (no tree existed);
    # shaping then reparses once. A dict is plain JSON data -- persisted in the
    # spill, read by response shaping, and NEVER part of the wire response
    # (build_unified_result maps it into the existing PageMetadata fields).
    llm_metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _check_literal_fields(self)



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
