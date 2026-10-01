from __future__ import annotations

import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING, Any, Literal, NewType, Protocol, Union, get_args, get_origin

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
    # strict enum field at response shaping.
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

# ``X | Y`` and ``Union[X, Y]`` are the same type with two spellings and two
# origins; a taxonomy alias written either way has to read the same here.
_UNION_ORIGINS = (Union, types.UnionType)


def _literal_members(alias: Any) -> frozenset[str]:
    """The string members of a declared type, whatever shape it composes in.

    The one place a declared type crosses into a runtime membership check, and
    the reason it is total rather than a ``get_args`` call: ``get_args`` answers
    different questions for different annotations. For a bare ``Literal`` it
    returns the members; for a union it returns the union's arguments
    UNflattened, so the members arrive as nested alias objects; and for
    ``dict[str, str]`` it returns ``(str, str)``, which a naive recursive walk
    reads as two members named "str".

    So the origin decides. A ``Literal`` contributes its string args, a union
    contributes the union of what it holds (which is what lets ``CrawlError``
    below be *defined* as a union of the two taxonomies instead of restating one
    of them), and anything else is not a taxonomy and contributes nothing. That
    last clause is the load-bearing one: it is what makes this safe to point at
    an arbitrary field annotation, which is what a field sweep does.
    """
    origin = get_origin(alias)
    if origin is Literal:
        return frozenset(arg for arg in get_args(alias) if isinstance(arg, str))
    if origin in _UNION_ORIGINS:
        members: set[str] = set()
        for arg in get_args(alias):
            members |= _literal_members(arg)
        return frozenset(members)
    return frozenset()


# The literal members as a runtime set. ``Classification`` crosses a string
# boundary in a few places (an exception message carrying a taxonomy code, a
# cached dict value), and narrowing one back to the Literal needs a membership
# check against exactly these values -- derived from the alias so the set and
# the enum cannot drift.
CLASSIFICATION_MEMBERS: frozenset[str] = _literal_members(Classification)


_CLASSIFICATION_TABLE: dict[str, Classification] = {name: name for name in get_args(Classification)}


def as_classification(value: str, *, default: Classification = "unknown_error") -> Classification:
    """Return ``value`` as a:data:`Classification`, or ``default`` if it is not one.

    Callers that pull a code off a string boundary (an exception message, a
    cached dict value) route through here rather than asserting, so an
    untriaged code degrades to the documented default instead of widening the
    classification type at every consumer. The table is built from the alias's
    own members so the mapping and the enum cannot drift; its values are the
    Literal members, which is what makes the return provably narrow.
    """
    return _CLASSIFICATION_TABLE.get(value, default)


# Pipeline conditions with no page-classification analogue: a capacity decision
# and a config one. They are not page outcomes, so forcing them into
# Classification would distort that taxonomy.
PIPELINE_ONLY_CRAWL_ERRORS = Literal[
    # Deliberately not spelled like its ``robots_disallowed`` Classification
    # member: this is the stable ERROR code, which may differ from the
    # classification of the same event, and the taxonomy invariant asserts the
    # two sets stay disjoint. ``request_pipeline._robots_disallowed`` sets the
    # pair (``classification="robots_disallowed"``,
    # ``error="robots_txt_disallowed"``); the two strings are not interchangeable.
    "robots_txt_disallowed",
    "browser_worker_unavailable",
    "unsupported_strategy",
    "retries_exhausted",
]

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
#
# It is the UNION of the two, not a restatement of the first. mypy cannot
# evaluate ``*get_args()`` inside ``Literal[...]``, so this used to spell every
# Classification member out again and pin the copy with a test -- a hand-kept
# mirror of the alias three lines above it, held in agreement by a guard that had
# to be re-derived every time a code was added. A union of two aliases is
# something mypy can evaluate and get_args cannot flatten on its own, which is
# what _literal_members above is for: the static type, the runtime member set, and
# the taxonomy are now the same declaration read three ways.
CrawlError = Classification | PIPELINE_ONLY_CRAWL_ERRORS

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
    "status": set(_literal_members(ResultStatus)),
    "classification": set(_literal_members(Classification)),
    "selected_strategy": set(_literal_members(CrawlStrategyName)),
    "strategy": set(_literal_members(CrawlStrategyName)),
    "error": set(_literal_members(CrawlError)),
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


class _LiteralChecked:
    """The construction-time taxonomy check, inherited rather than re-spelled.

    Every result dataclass below declares fields whose annotation is a taxonomy
    ``Literal``, so every one of them has to run :func:`_check_literal_fields` at
    construction. That check is one function, but the ``__post_init__`` that calls
    it was a two-line trampoline repeated on each class, which is a second place
    to forget: a result dataclass added tomorrow would validate nothing and read
    exactly like the ones that do. Inheriting makes the check part of the class
    rather than something it remembers to ask for, and it leaves one spelling of
    it in the tree for a guard to point at.

    Subclassing note: defining ``__post_init__`` on a subclass would *override*
    this one, so a result dataclass that grows a post-init of its own has to call
    ``super().__post_init__()``. The taxonomy check is the reason the base
    exists, so losing it silently is the one outcome that must not happen.
    """

    def __post_init__(self) -> None:
        _check_literal_fields(self)


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
class CrawlResult(_LiteralChecked):
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
class CrawlStrategyAttempt(_LiteralChecked):
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


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    # The frontier/visited-eligible form; producers build it from
    # ``normalize_public_http_url`` output (``frontier_link_url`` is the only
    # read allowed for queueing -- the raw ``url`` href stays unqueue-able).
    normalized_url: NormalizedUrl
    text: str
    rel: list[str]
    source_url: str
    host: str
    is_same_host: bool


# The canonical form produced by ``app.validation.normalize_public_http_url``
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
class EngineCrawlResult(_LiteralChecked):
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
    # shared discovery tree. None = never computed here (no tree existed);
    # shaping then reparses once. A dict is plain JSON data -- persisted in the
    # spill, read by response shaping, and NEVER part of the wire response
    # (build_unified_result maps it into the existing PageMetadata fields).
    llm_metadata: dict[str, Any] | None = None

    @property
    def elapsed_ms(self) -> int:
        """How long this result's work took, or 0 when nothing observed it.

        The scheduler is generic over two result shapes and needs this number
        from both, so it is one attribute with one meaning rather than a
        ``getattr`` for a member only one of the two happens to carry. The
        diagnostics block is consulted first because that is what the HTTP and
        browser strategies have always written, and the attempt is the fallback
        for a result built without diagnostics (the worker-exception and
        budget-timeout envelopes). Both are milliseconds, so no caller has to
        know which one it got.
        """
        runtime = self.diagnostics.get("runtime")
        if isinstance(runtime, dict):
            observed = runtime.get("elapsed_ms")
            if isinstance(observed, int | float):
                return int(observed)
        if self.attempted_strategies:
            return int(self.attempted_strategies[0].elapsed_ms)
        return 0


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
# The concrete fetcher accepts the content-length cap and the retry wrapper passes
# it positionally, so the alias must describe four parameters -- a caller typed
# against it must be able to invoke the real fetcher it is handed.
#
# A Protocol rather than a ``Callable[[str, dict[str, str], int, int], Any]``
# alias, for two reasons that are both load-bearing. The fourth parameter is
# optional in every implementation and a Callable alias cannot say so -- it types
# all four as required -- and ``functools.partial`` drops the default when it
# synthesizes a signature, so a correct three-argument call read as a missing
# argument. The return is the result model, not ``Any``.
#
# This is the ONLY declaration of the fetcher call shape. A second Protocol for
# the same four parameters, plus a keyword-only ``policy`` and an ``impersonate``
# keyword, used to sit here beside it -- the bound view and the concrete-fetcher
# contract -- and Python cannot derive one from the other, because a function
# with a *required* keyword-only argument does not satisfy a Protocol that does
# not require it. So the four lines were written twice and could only be kept in
# step by a guard that compared each half against a third thing.
#
# The bound view is the right one to keep: it is the narrower declaration, and it
# is the one a call site needs. The keywords a production fetcher adds on top of
# it are not a second contract, they are a fact about each fetcher, and
# ``tests/test_type_contracts.py::test_http_fetcher_contract_holds_tree_wide``
# checks them where they are declared: every coroutine in ``app/`` returning a
# fetch result must take exactly the keyword-only ``policy`` (required) and
# ``impersonate``, with the same four positional parameters and the same
# optionality. Both halves are load-bearing, and the defaulting half was found the
# hard way: a fetcher that gave ``policy`` a default matched every name, because
# protocol conformance is one-directional and a default changes no name -- so a
# missing SSRF keyword would have become an unrestricted HostPolicy instead of a
# TypeError, silently.
class HttpFetcher(Protocol):
    """The four positional parameters of a fetcher whose collaborators are already bound.

    Also the declaration of what a fetcher looks like *before* it is bound, which
    is the only sense in which this is a contract for the whole of ``app/``: a
    production fetcher is this call plus a keyword-only ``policy``, an
    ``impersonate`` keyword and a ``pin`` keyword, and the tree-wide check is
    what proves the "plus" is exactly those three.

    ``pin`` is the transport's pin rule, and ``None`` -- the default -- means
    the real one. It is declared here rather than kept off the contract because
    the guard compares defaulting as well as names, and a binding a contract
    cannot see is a binding that drifts. Its safety rests on that default:
    omitting it cannot substitute a weaker rule, so unlike ``policy`` it is
    optional in the harmless direction.
    """

    async def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout_seconds: int,
        max_content_length: int = ...,
    ) -> HttpFetchResult: ...


def to_dict(result: Any) -> dict[str, Any]:
    """Return a JSON-serializable dict for any frozen-dataclass crawl result.

    Unlike ``dataclasses.asdict``, this does NOT ``deepcopy`` leaf values. The
    heavy fields (``html``/``text`` strings, ``metadata``/``diagnostics``
    dicts) are shared by reference, and only nested dataclasses (e.g.
    ``DiscoveredLink``) are converted to dicts -- so the output stays
    JSON-serializable without paying asdict's full recursive deep-copy cost on
    every field of every result in a large batch.
    """
    return _to_jsonable(result)


def _to_jsonable(value: Any) -> Any:
    return _to_jsonable_impl(value, set())


def _entered(value: Any, seen: set[int]) -> set[int] | None:
    """The seen-set to recurse into ``value`` with, or None if it is already on the path.

    Only mutable containers can take part in a reference cycle, which is why the
    primitive types skip it entirely. :func:`_build_guarded` is its only caller:
    a container kind added to ``_to_jsonable_impl`` arrives as a new
    ``isinstance`` arm, and nothing about adding one makes this check mandatory
    there, so the check belongs on the arm's shared path rather than in each arm.
    """
    oid = id(value)
    if oid in seen:
        return None
    return seen | {oid}


def _build_guarded(value: Any, seen: set[int], build: Callable[[Any, set[int]], Any]) -> Any:
    """Register ``value`` on the path, then let ``build`` render its children.

    ``build`` is given the value and the descended path set, and is never called
    for a value already on that path -- that call returns None instead, which is
    how a cyclic reference is broken rather than followed.
    """
    entered = _entered(value, seen)
    if entered is None:
        return None
    return build(value, entered)


def _to_jsonable_impl(value: Any, seen: set[int]) -> Any:
    # Cyclic graphs (e.g. a DiscoveredLink whose parent/children point back to
    # an ancestor) recurse forever without a check, and a RecursionError at
    # serialization time is far from the code that caused it. Every arm below
    # descends through _build_guarded, so the check cannot be forgotten by a
    # new one. Primitives skip it: they cannot cycle, and small strings may be
    # interned and share an id across the tree, so flagging them would truncate
    # valid results.
    if is_dataclass(value) and not isinstance(value, type):
        return _build_guarded(
            value,
            seen,
            lambda node, path: {f.name: _to_jsonable_impl(getattr(node, f.name), path) for f in fields(node)},
        )
    if isinstance(value, dict):
        return _build_guarded(
            value, seen, lambda node, path: {key: _to_jsonable_impl(item, path) for key, item in node.items()}
        )
    if isinstance(value, (list, tuple)):
        return _build_guarded(
            value, seen, lambda node, path: [_to_jsonable_impl(item, path) for item in node]
        )
    return value


class BrowserPoolExhaustedError(Exception):
    """No renderer became available within the acquire timeout.

    Lives in ``app.models`` (not ``browser_pool``) because the shared error
    classifier in ``app.crawl.result_helpers`` must isinstance-check it
    without importing a strategy module: browser_pool imports browser, and
    browser imports result_helpers, so defining it there would create an
    import cycle. ``browser_pool`` re-exports it for its existing importers.
    """
