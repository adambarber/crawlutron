"""Single source of truth for policy/strategy/extraction magic strings.

These literals are consumed by ``app.api_models``,
``app.discovery`` and the test suite so that spelling drift (or a stray
non-ASCII look-alike character) cannot desynchronize comparisons. Always
import the constant instead of typing the string literal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal
from urllib.parse import urlparse as _urlparse

# Fetch strategies (how bytes are retrieved). Orthogonal to extraction.
# ---------------------------------------------------------------------------
HTTP_DECLARED_CRAWLER: Final = "HTTP_DECLARED_CRAWLER"
HTTP_CFFI_IMPERSONATE: Final = "HTTP_CFFI_IMPERSONATE"
BROWSER_HEADLESS: Final[Literal["BROWSER_HEADLESS"]] = "BROWSER_HEADLESS"

# Service version: single source of truth, imported directly by callers.
SERVICE_VERSION: str = "0.3.0"
# The service's own id, as callers and clients see it. This string was spelled
# at seven sites -- the three discovery payloads, the MCP server name, the
# OpenAPI service_name and its contact name, and the user-agent token -- and
# they all answer for the same service, so a rename that missed one would make
# the wire describe something other than what replied. One constant, seven
# references: a site that drifts is now a visible edit rather than a silent lie.
SERVICE_ID: Final = "crawlutron"
ROUTER_MODULE_DOC: Final = (
    "Pure router: imports :func:`{handler}` and binds it\n"
    "to the ``{path}`` POST path. The handler implementation lives in the\n"
    "endpoint module so the router stays trivial and the handler is testable\n"
    "without FastAPI machinery."
)
# The project's canonical remote. It goes out on the wire in the user-agent
# below, so a placeholder here is not a documentation wart: it tells every host
# the crawler visits that a repository which does not exist is where to find
# the code. pyproject.toml needs the same value and cannot import this, so
# tests/test_project_urls.py asserts the two agree rather than trusting a copy
# to stay in step.
REPOSITORY_URL: Final = "https://github.com/adambarber/crawlutron"
# Public-facing identity strings. Kept here so the same UA appears in both the
# declared-crawler header and the synthetic ``User-Agent:`` line we attach
# to outbound requests, and so the package version is sourced from a single
# place instead of being duplicated across files.
# Overridable via CRAWLUTRON_USER_AGENT.
DEFAULT_USER_AGENT: str = f"{SERVICE_ID}/{SERVICE_VERSION} (+{REPOSITORY_URL})"
USER_AGENT_TOKEN: str = SERVICE_ID
# TLS-fingerprint target for the HTTP_CFFI_IMPERSONATE strategy: the
# fetcher and the agent-hints copy both reference this, so they cannot drift.
CFFI_IMPERSONATE_TARGET: str = "firefox135"
BROWSER_HEADED_WARMED: Final[Literal["BROWSER_HEADED_WARMED"]] = "BROWSER_HEADED_WARMED"

# Strategy ordering / registries (imported by service.py)
HTTP_STRATEGIES: set[str] = {HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE}
BROWSER_STRATEGIES: set[str] = {BROWSER_HEADLESS, BROWSER_HEADED_WARMED}
SUPPORTED_STRATEGIES: set[str] = HTTP_STRATEGIES | BROWSER_STRATEGIES
DEFAULT_STRATEGIES: list[str] = [
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
]

# ---------------------------------------------------------------------------
# Extraction methods (how retrieved bytes are parsed into text/markdown).
# These are output-pipeline options, strictly orthogonal to fetch strategies.
# ---------------------------------------------------------------------------
EXTRACTION_FULL: str = "full"
EXTRACTION_TRAFILATURA: str = "trafilatura"
EXTRACTION_DOCUMENT: str = "document"
EXTRACTION_DOM: str = "dom"

SUPPORTED_EXTRACTIONS: list[str] = [
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
]

# Human-readable descriptions for discovery/agent-hints.
EXTRACTION_DESCRIPTIONS: dict[str, str] = {
    EXTRACTION_FULL: "Unified output pipeline: trafilatura HTML main text, "
    "markitdown for documents, plain-text passthrough.",
    EXTRACTION_TRAFILATURA: "HTML-only main-text extraction for noisy HTML; "
    "does not parse PDF or binary documents.",
    EXTRACTION_DOCUMENT: "Document/body extraction via markitdown for PDFs and "
    "non-HTML responses when text evidence is needed.",
    EXTRACTION_DOM: "DOM-text extraction: rendered or parsed page text passthrough "
    "without the document or Trafilatura pipelines.",
}

# ---------------------------------------------------------------------------
# extraction_method result codes (diagnostic/provenance labels in crawl results).
# ---------------------------------------------------------------------------
EXTRACTION_METHOD_HTTP_HTML: str = "http_html"
EXTRACTION_METHOD_HTTP_BODY: str = "http_body"
EXTRACTION_METHOD_DOCUMENT: str = "document_extract"
EXTRACTION_METHOD_TRAFILATURA: str = "trafilatura_extract"
EXTRACTION_METHOD_DOM_TEXT: str = "dom_text"
EXTRACTION_METHOD_BINARY_NO_TEXT: Final = "binary_no_text"


# ---------------------------------------------------------------------------
# The classification taxonomy: THE single source of truth.
#
# Every classification any strategy can emit is a member of this enum, and the
# groupings below are derived from it (member references, not a second string
# copy) so adding a classification is a one-file change. The response models
# import this enum directly; the /v1/capabilities advertisement is generated
# from it.
# ---------------------------------------------------------------------------


class CrawlClassification(StrEnum):
    """Normalized result classifications used across HTTP, browser, and document strategies."""

    rendered = "rendered"
    captcha_detected = "captcha_detected"
    bot_challenge_detected = "bot_challenge_detected"
    access_denied = "access_denied"
    login_required = "login_required"
    paywall_detected = "paywall_detected"
    rate_limited = "rate_limited"
    cookie_consent_required = "cookie_consent_required"
    javascript_required = "javascript_required"
    unsupported_browser = "unsupported_browser"
    geolocation_blocked = "geolocation_blocked"
    waf_challenge_detected = "waf_challenge_detected"
    not_found = "not_found"
    site_error = "site_error"
    gone = "gone"
    binary_no_text = "binary_no_text"
    robots_disallowed = "robots_disallowed"
    navigation_timeout = "navigation_timeout"
    browser_crash = "browser_crash"
    network_error = "network_error"
    connection_error = "connection_error"
    http_status_error = "http_status_error"
    # The per-URL budget's own signal: the page was alive but slower than the
    # caller's total_url_budget_seconds. Genuinely distinct -- it is not
    # network_error (the host answered) and not navigation_timeout (the whole
    # chain overran, not one navigation). Was missing from the enum while the
    # engine already emitted it as known_classification=, so the lenient
    # internal dataclass accepted it and the strict enum at response shaping
    # rejected the entire batch with a 500.
    url_budget_exceeded = "url_budget_exceeded"
    # Strategy-level failure codes emitted by the shared error classifier
    # (``app.crawl.result_helpers._classify_error_code``). They are members of
    # the taxonomy (not internal-only strings) because they are stored verbatim
    # in ``CrawlStrategyAttempt.classification`` and this field is the strict
    # enum at response shaping: a non-member code sails through the lenient
    # internal dataclass and then rejects the whole batch with a 422.
    #
    # browser_pool_exhausted is deliberately distinct from network_error: it
    # means the URL was never attempted because renderer capacity was
    # unavailable, so the page is a retry candidate, not a dead page. The
    # classifier and the retry decision both key off this label.
    browser_pool_exhausted = "browser_pool_exhausted"
    # A domain's breaker was open, so the URL was never attempted: like
    # browser_pool_exhausted this is a capacity decision, not a dead page. A
    # member of the taxonomy so the classifier's ``circuit_breaker_open`` code
    # survives the strict enum at response shaping instead of being downgraded
    # to site_error (which would make it indistinguishable from a real failure
    # and hide the reason the attempt was skipped).
    circuit_breaker_open = "circuit_breaker_open"
    payload_too_large = "payload_too_large"
    too_many_redirects = "too_many_redirects"
    http_error = "http_error"
    value_error = "value_error"
    tls_error = "tls_error"
    dns_error = "dns_error"
    invalid_url = "invalid_url"
    unsupported_scheme = "unsupported_scheme"
    private_network_target = "private_network_target"
    unknown_error = "unknown_error"
    not_modified = "not_modified"


# ---------------------------------------------------------------------------
# Classification groupings (consumed by HTTP/browser strategy status mappers
# and the test suite). Centralized so the HTTP vs browser blocked sets cannot
# silently drift apart across modules.
#
# These are set OPERATIONS over one another, not one transcription of the
# taxonomy per grouping. Naming the members is allowed exactly once each, in the
# leaf grouping that is about them; everything downstream composes. That is the
# property the previous four hand-written lists did not have: ``HTTP_BLOCKED``
# was already a union, so a new blocked label reached it for free, while
# ``NON_TRANSIENT`` re-listed the same eleven labels and a new one had to be
# added there by hand -- with nothing failing when it was not, because a
# frozenset that is merely missing a member is still a valid frozenset.
# ``test_a_classification_grouping_is_derived_not_restated`` is the drift guard.
#
# Deriving from the enum also means a grouping can never name a label the
# taxonomy does not know about: a typo is an AttributeError at import.
# ---------------------------------------------------------------------------
def _classification_values(*members: CrawlClassification) -> frozenset[str]:
    """The label set for a leaf grouping declared as enum members.

    Every grouping below that is not a leaf is a ``|`` of earlier ones, so this
    is called with a member only in the one place that member is being
    classified, and a member is never spelled twice.
    """
    return frozenset(member.value for member in members)


# The anti-bot / consent / access walls a page can be behind. The base of every
# blocked grouping below.
BROWSER_BLOCKED_CLASSIFICATIONS = _classification_values(
    CrawlClassification.captcha_detected,
    CrawlClassification.bot_challenge_detected,
    CrawlClassification.access_denied,
    CrawlClassification.login_required,
    CrawlClassification.paywall_detected,
    CrawlClassification.rate_limited,
)

# The walls only the HTTP path can report, because only it fetches without a
# renderer. Named separately so ``HTTP_BLOCKED`` reads as the superset it is.
_HTTP_ONLY_CLASSIFICATIONS = _classification_values(
    CrawlClassification.cookie_consent_required,
    CrawlClassification.javascript_required,
    CrawlClassification.unsupported_browser,
    CrawlClassification.geolocation_blocked,
    CrawlClassification.waf_challenge_detected,
)

HTTP_BLOCKED_CLASSIFICATIONS = BROWSER_BLOCKED_CLASSIFICATIONS | _HTTP_ONLY_CLASSIFICATIONS

# Content was retrieved but yielded nothing usable (HTTP/browser status mappers
# and the browser strategy's retry hints all key off this group).
FAILED_CLASSIFICATIONS = _classification_values(
    CrawlClassification.not_found,
    CrawlClassification.site_error,
    CrawlClassification.gone,
    CrawlClassification.binary_no_text,
)

# ---------------------------------------------------------------------------
# Crawl result status (the high-level outcome of a URL). Declared here because
# :data:`CLASSIFICATION_STATUS_BY_LABEL` below maps every classification onto
# one of these, and that table is the low-level constant. ``app.models``
# re-exports the alias so dataclass fields keep a single name.
# ---------------------------------------------------------------------------
ResultStatus = Literal["success", "blocked", "disallowed", "retry_exhausted", "failed", "skipped"]

# ---------------------------------------------------------------------------
# Classification -> result status: THE single source of truth.
#
# This used to be two independent if-chains (``http._status_for_classification``
# and ``BrowserRenderer._status_for_classification``) that agreed on the labels
# they both recognized and disagreed on everything else: an unrecognized label
# came back "failed" from the HTTP path and "blocked" from the browser path, so
# the status a caller saw depended on which fetcher served the page. Both call
# sites now delegate to :func:`status_for_classification`.
#
# Kept as a table rather than branches because it is also the seed of the
# future per-classification metadata (retryability, escalation hints, metric
# buckets): new columns land next to the status, not inside two conditionals.
# ---------------------------------------------------------------------------
CLASSIFICATION_STATUS_BY_LABEL: dict[str, ResultStatus] = {
    # Usable content, or freshness proved by the caller's own validators.
    "rendered": "success",
    "not_modified": "success",
    # Anti-bot / consent / access walls: the page exists, we were kept out.
    **dict.fromkeys(HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS, "blocked"),
    # Retrieved, but nothing a consumer can use.
    **dict.fromkeys(FAILED_CLASSIFICATIONS, "failed"),
    # Fetch-level failure codes the HTTP strategy's error classifier emits.
    # These are known-dead outcomes, not unclassified blocks: the
    # strategy already reported status "failed" for the URL, and these must
    # not fall through to the "blocked" default, which would disagree with
    # the fetcher's own status and keep a dead page eligible for re-serve.
    # Only the three codes this defect added are listed here; the older
    # error codes (network_error/dns_error/...) pre-date this table's
    # completeness and their correct statuses are nuanced (e.g.
    # robots_disallowed is "disallowed", private_network_target is a policy
    # refusal), so they are deliberately left to their documented default
    # rather than being bulk-flipped to "failed" here.
    "payload_too_large": "failed",
    "too_many_redirects": "failed",
    "http_error": "failed",
    "url_budget_exceeded": "failed",
    # Capacity, not a dead page: the URL was never attempted because no
    # renderer was free. Reported as failed so the caller sees a terminal
    # result for this pass, but diagnostics["retryable"] is True and the
    # frontier layer re-queues it -- so this status describes the attempt,
    # not the page.
    "browser_pool_exhausted": "failed",
    # Capacity, not a dead page: the domain's breaker was open, so the URL was
    # never attempted. Same retry contract as browser_pool_exhausted above.
    "circuit_breaker_open": "failed",
    # Transport / strategy exception codes emitted by the shared classifier
    # (``_classify_error_code``) and by the engine's unsupported-strategy
    # guard. Every one reaches the wire through the exception chokepoint
    # (``_engine_result_from_exception`` / ``_crawl_result_from_exception``),
    # which builds a terminal *failure*, so their status is "failed" -- not
    # the "blocked" default an unlisted label takes. "blocked" is the
    # non-assertive "we were kept out" verdict; these mean the fetch itself
    # died, and leaving them unlisted made a consumer re-deriving status from
    # classification disagree with the wire.
    "network_error": "failed",
    "connection_error": "failed",
    "tls_error": "failed",
    "dns_error": "failed",
    "http_status_error": "failed",
    "value_error": "failed",
    "browser_crash": "failed",
    "unknown_error": "failed",
    # A robots.txt disallow is a deliberate non-attempt, not a page failure and
    # not a block: the crawl path reports "skipped" (``_skipped_engine_result``),
    # which says "we chose not to fetch" rather than "we could not", and keeps
    # the URL out of the failed-bookkeeping a "failed" status would feed.
    "robots_disallowed": "skipped",
    # Policy refusals from ``normalize_public_http_url`` (unsupported scheme,
    # non-public target, malformed URL), surfaced by the render endpoint as a
    # caller-visible refusal. Never a page verdict of any kind.
    "invalid_url": "disallowed",
    "unsupported_scheme": "disallowed",
    "private_network_target": "disallowed",
    # The browser retry loop exhausted its attempts. Distinct from a transport
    # failure: the page may be perfectly renderable on a later pass, so the
    # verdict describes the attempt, not the page.
    "navigation_timeout": "retry_exhausted",
}

# Unknown-classification default: "blocked".
#
# WHY this is the safe one for a crawler:
#  * It must never be "success". Success is the only status that gets a
#    strategy pinned for the whole domain, feeds quality scoring, and is served
#    from the content cache; defaulting an unrecognized label there would let a
#    taxonomy change silently launder unknown pages into cached "good" results.
#  * "failed" is an ASSERTIVE verdict -- it is what a page gets when the
#    taxonomy knows it is dead (404/410/5xx/no-text) and it feeds job
#    ``total_failed``. An unknown label is a label nobody has triaged yet, and
#    reporting "this page is dead" because our enum is missing an entry
#    discards content that may be perfectly crawlable on the next pass.
#  * "blocked" is the non-assertive "we did not get usable content, reason
#    unclassified" state: it is visible in results, keeps the URL eligible to be
#    revisited, and cannot poison caches or strategy pinning. A label that
#    deserves "failed" only ever reaches the default if someone forgot to list
#    it -- which ``CLASSIFICATION_STATUS_BY_LABEL`` completeness tests pin.
UNKNOWN_CLASSIFICATION_STATUS: ResultStatus = "blocked"


def status_for_classification(classification: str) -> ResultStatus:
    """Map a page classification onto the crawl result status.

    Every classification a strategy can emit is an explicit key in
    :data:`CLASSIFICATION_STATUS_BY_LABEL`; only labels outside the taxonomy
    (or a classifier that has not been given a status) take
    :data:`UNKNOWN_CLASSIFICATION_STATUS`.
    """
    return CLASSIFICATION_STATUS_BY_LABEL.get(classification, UNKNOWN_CLASSIFICATION_STATUS)


# ---------------------------------------------------------------------------
# Classification transience: is a page that failed this way worth another pass?
# ---------------------------------------------------------------------------
# The second axis of the taxonomy, beside the status table above. Status answers
# "what do we tell the caller"; transience answers "should we come back", and the
# two are independent -- a capacity skip and a page-level 404 are both "failed"
# but only one deserves a retry.
#
# WHY the taxonomy owns it rather than a local constant: three call sites each
# answered "is this transient?" with their own hardcoded list and they had
# already drifted. The browser strategy authorized retries for ``dns_error`` -- a
# code nothing produced -- while omitting ``connection_error``, which is the label
# the classifier actually emits for every DNS, connect, timeout and reset
# failure. The durable job runner imported that same set to decide whether to
# re-queue, so a one-second DNS blip marked the URL visited and dropped it from a
# whole-site crawl. A fourth answer would appear next sprint for the same reason.
#
# The rule, so membership is decidable rather than tasteful: TRANSIENT is for
# codes where our fetch died without forming a verdict about the page, or where
# the verdict was a statement about our own capacity. A page the origin
# characterized is terminal; re-queueing one of those either re-attempts what
# robots forbids or hammers an origin that just refused us.
TRANSIENT_CLASSIFICATIONS: frozenset[str] = frozenset(
    member.value
    for member in (
        # Transport died before any verdict about the page.
        CrawlClassification.network_error,
        CrawlClassification.connection_error,
        CrawlClassification.dns_error,
        CrawlClassification.tls_error,
        CrawlClassification.http_error,
        CrawlClassification.too_many_redirects,
        # A deadline overran: the page was alive, just slow, so a later pass can
        # hit a warm cache or a quieter moment.
        CrawlClassification.navigation_timeout,
        CrawlClassification.url_budget_exceeded,
        # Capacity, not a dead page: the URL was never attempted. The status
        # table already says "failed" for both; this says the page is still
        # eligible, which is what the frontier re-queue consumes.
        CrawlClassification.browser_pool_exhausted,
        CrawlClassification.circuit_breaker_open,
        # A renderer died rather than a page.
        CrawlClassification.browser_crash,
        # Untriaged: the one case where another pass is pure upside.
        CrawlClassification.unknown_error,
    )
)
# The other side, stated rather than left to a default so the taxonomy stays
# fully covered: a frozenset of the affirmative cases alone cannot tell "we
# decided this is terminal" from "nobody has triaged this yet", so a new member
# would silently inherit whichever default the set implied. The pair's union is
# the complete verdict the completeness test derives from -- the same discipline
# as CLASSIFICATION_STATUS_BY_LABEL above.
#
# robots_disallowed is the clearest no: re-queueing it re-attempts exactly what
# the policy forbids. rate_limited is the breaker and the politeness floor's
# lever, not ours -- a second immediate attempt is the abusive hammer. site_error
# and http_status_error are deliberate origin responses, so the per-URL budget
# must not fight the domain breaker's backoff by re-queueing underneath it.
# payload_too_large is deterministic: the next fetch downloads the same oversized
# body. value_error and the three policy refusals are ours, not the page's -- they
# resolve only when the caller changes. The refusal and capability members
# (access_denied, login_required, paywall, captcha, bot and WAF challenges,
# cookie consent, javascript_required, unsupported_browser, geolocation) are
# pages the origin characterized, or capabilities we chose not to use. not_found
# and gone are statuses the origin chose. rendered and not_modified are not
# failures at all.
NON_TRANSIENT_CLASSIFICATIONS: frozenset[str] = (
    HTTP_BLOCKED_CLASSIFICATIONS
    | FAILED_CLASSIFICATIONS
    # Fetch-level refusals and the pipeline's own errors. Ours, not the page's:
    # they resolve only when the caller changes.
    | _classification_values(
        CrawlClassification.robots_disallowed,
        CrawlClassification.payload_too_large,
        CrawlClassification.http_status_error,
        CrawlClassification.value_error,
        CrawlClassification.invalid_url,
        CrawlClassification.unsupported_scheme,
        CrawlClassification.private_network_target,
    )
    # Not failures at all: a page that rendered, and a page the caller's own
    # validators proved fresh.
    | _classification_values(
        CrawlClassification.rendered,
        CrawlClassification.not_modified,
    )
)


def is_transient_classification(classification: str | None) -> bool:
    """Return whether a page that failed as ``classification`` deserves another pass.

    The single answer for every retry decision: the render layer's
    503-vs-permanent split, the crawl layer's ``diagnostics["retryable"]`` flag,
    the recursive crawler's re-queue, and the durable job runner's per-URL
    transient budget all route through here.

    Fails closed, and ``None`` with it: a missing or unrecognized label -- a
    result row that never carried a classification, or a new taxonomy member
    nobody has triaged -- is not retried, because retrying an untriaged failure
    is the direction that costs politeness.
    """
    return classification in TRANSIENT_CLASSIFICATIONS


# The exhaustive set of classifications whose result status is NOT determined
# by the label alone, each mapped to the statuses a builder may legitimately
# pair it with beyond the table default.
#
# ``unknown_error`` is the one such label: the engine's unsupported-strategy
# guard and the exception chokepoint report it as the table default "failed",
# but the browser retry loop reports "retry_exhausted" once it has spent every
# attempt -- a verdict about the attempt that the classification cannot carry.
# Every entry here is a load-bearing, context-dependent status, not a drift
# source: the terminal-result builders call
# :func:`status_agrees_with_classification` on every construction, so a pairing
# that is neither the table default nor listed here raises instead of reaching
# the wire, and a new context-dependent pairing must be added here in the open.
CONTEXT_VARYING_STATUS: dict[str, frozenset[ResultStatus]] = {
    "unknown_error": frozenset({"retry_exhausted"}),
}


def status_agrees_with_classification(classification: str, status: str) -> bool:
    """Whether a ``(classification, status)`` pair is one the taxonomy allows.

    This is the guard that makes a disagreeing pair unrepresentable at the
    terminal-result chokepoint: ``status`` must either be the table default for
    this classification (:func:`status_for_classification`) or one of the
    documented context-dependent statuses in :data:`CONTEXT_VARYING_STATUS`.
    A caller re-deriving status from classification can therefore rely on the
    value the builder set, which is the load-bearing invariant in this
    module's docstring.
    """
    if status == status_for_classification(classification):
        return True
    return status in CONTEXT_VARYING_STATUS.get(classification, frozenset())


# ---------------------------------------------------------------------------
# Crawl policy limit defaults and hard caps.
#
# Single source of truth for the numeric limits that previously lived as bare
# literals / inline os.environ reads across api_models, payload_helpers,
# http.py, main.py, discovery/* and browser.py. Change a limit HERE, not in N
# places. The api_models pydantic Fields reference these so the advertised
# contract (``/v1/capabilities``) and the runtime defaults cannot drift.
# ---------------------------------------------------------------------------
# Discovered links returned per selected page.
DEFAULT_MAX_DISCOVERED_LINKS: int = 100
# Hard cap on the field AND the default for the extreme /v1/map whole-site
# discovery path (intentionally the maximum, not the conservative 100).
MAX_DISCOVERED_LINKS: int = 2000

# Passive browser challenge observation budget (seconds).
DEFAULT_CHALLENGE_WAIT_SECONDS: int = 15

# Per-response body size bound.
DEFAULT_MAX_CONTENT_LENGTH_BYTES: int = 10 * 1024 * 1024
# Hard ceiling a caller may set for a single page's body cap. Above it an
# explicit ``max_content_length_bytes`` would disable the concurrency-scaled
# RAM budget and inflate the decompression bound — so it is rejected, not
# honored (matches max_rps). Set to the default total RAM budget: one page
# may never outgrow the whole in-flight allowance.
MAX_CONTENT_LENGTH_BYTES: int = 1 * 1024 * 1024 * 1024
DISCOVERY_BODY_MAX_BYTES: int = 8 * 1024 * 1024

# Per-strategy timeout / retry budgets.
DEFAULT_HTTP_TIMEOUT_SECONDS: int = 30
# The caller's ceiling on a per-strategy timeout. Declared here so the model's
# `le` and the deployment default's bound are one number: the settings comment
# claimed they were "the same declaration" while the deployment side was a
# literal `hi=120`, which is exactly the pair that drifts.
MAX_HTTP_TIMEOUT_SECONDS: int = 120

# Three retry numbers, three authorities, deliberately not interchangeable:
#   * DEFAULT_MAX_RETRIES -- what a job gets when nobody states a limit.
#   * MAX_RETRIES -- the most an OPERATOR may install as the deployment-wide
#     default. The deployment is a blast radius shared by every caller, so it is
#     capped tightly and out-of-range is rejected rather than quietly rewritten.
#   * MAX_JOB_MAX_RETRIES -- the most ONE job's caller may ask for. Higher than
#     the deployment ceiling on purpose: a client that knows its target is
#     flaky may spend more retries on that one job, without the operator having
#     to open the deployment up for everyone.
# DEFAULT and the deployment ceiling coincide at 5 today. They are separate
# constants because "we default to 5" and "no deployment may exceed 5" are
# different decisions, and collapsing them would make loosening one silently
# loosen the other.
DEFAULT_MAX_RETRIES: int = 5
MAX_RETRIES: int = 5
MAX_JOB_MAX_RETRIES: int = 10

# Durable whole-site retry cap: how many times a frontier batch may hand a URL to
# the engine when it fails for a TRANSIENT reason (network/dns/tls/timeout/browser
# crash). Such a URL is re-queued, not marked visited, so a network outage can't
# drain a whole-site job to a seed-only "completed". After this many attempts the
# URL is recorded terminal (error="retries_exhausted") so a permanently-dead page
# can't spin the frontier forever. Distinct from MAX_RETRIES (in-call strategy
# retries); this bounds ACROSS-batch durable retries.
MAX_URL_TRANSIENT_ATTEMPTS: int = 3

# Hard per-URL ceiling over the ENTIRE strategy chain (all strategies x retries
# plus browser challenge waits). Guards against poisoned/tarpit URLs that would
# otherwise hold a concurrency slot for many minutes while the resource gate
# reports idle capacity. Generous so it never trips on healthy targets.
DEFAULT_TOTAL_URL_BUDGET_SECONDS: float = 180.0
# The single source for large-crawl guidance. It shipped once as three
# hand-maintained copies (capabilities, agent-hints, the OpenAPI endpoint
# description); one constant feeds every surface, so the advice cannot drift
# between the places an agent reads it.
LARGE_CRAWL_GUIDANCE: Final[str] = (
    "Synchronous POST /v1/crawl has no server-side time ceiling: duration scales with "
    "max_depth, max_pages, per-domain politeness delays, and the per-URL "
    "total_url_budget_seconds (which fails one tarpit URL, not the crawl). For large or "
    "recursive crawls set async_mode=true (or webhook_url) for an immediate 202 with a "
    "status_url to poll at GET /v1/crawl/{request_id}, or use POST /v1/jobs for durable "
    "resumable whole-site jobs; job progress persists and supports pause/cancel/resume "
    "across restarts."
)
MAX_TOTAL_URL_BUDGET_SECONDS: float = 900.0
# Upper bound for the per-domain token-bucket rate limiter (CrawlPolicyLimits
# max_rps). Mirrors the API model's le=100.0 bound so the normalized policy
# path and the pydantic path reject the same values.
MAX_RATE_LIMIT_RPS: float = 100.0

# Cache TTLs (seconds).
DEFAULT_CACHE_TTL_SECONDS: float = 3600.0

# Disk content-cache byte budget. max_entries bounds entry count, not
# size: one envelope can hold a multi-MB base64 body, so without these caps
# 10k entries could exceed 100GB of disk. Entries over the per-entry cap
# are refused; LRU eviction enforces the total budget.
CONTENT_CACHE_MAX_ENTRY_BYTES: int = 4 * 1024 * 1024
CONTENT_CACHE_MAX_TOTAL_BYTES: int = 512 * 1024 * 1024

# Sitemap discovery tuning.
SITEMAP_MAX_DEPTH: int = 3
SITEMAP_MAX_URLS: int = 10_000
SITEMAP_CACHE_TTL_SECONDS: float = DEFAULT_CACHE_TTL_SECONDS
SITEMAP_CACHE_MAX_ENTRIES: int = 1000
SITEMAP_FANOUT_TIMEOUT_SECONDS: int = 45
# Maximum child-sitemap fetches in flight per index level. An index
# lists every child before any max_urls accounting, so without chunking a
# single hostile sitemapindex could spawn tens of thousands of tasks.
SITEMAP_INDEX_FANOUT: int = 16

# Engine scheduling: a recursive-crawl batch pulls global_concurrency * this.
ENGINE_BATCH_CONCURRENCY_MULTIPLIER: int = 2

# Browser runtime limits (also exposed as CrawlerSettings fields with
# CRAWLUTRON_* env mappings).
DEFAULT_MAX_BROWSER_RENDERS: int = 100
DEFAULT_MAX_CONCURRENT_PAGES: int = 3

# Browser fixed-overhead knobs (perf throughput #7, configurability only):
# lazy-load scroll repetitions, the pause between scrolls, and the bounded
# wait for the cookie harvest. Values equal the long-standing hardcoded
# behavior (3 steps / 1s / 5.0s); change them via CrawlerSettings/env or the
# per-request browser_profile, never by editing these defaults.
DEFAULT_LAZY_SCROLL_STEPS: int = 3
DEFAULT_LAZY_SCROLL_DELAY_SECONDS = 1
DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS: float = 5.0

# Browser pool: number of persistent browser instances to keep warm PER WORKER.
# Each browser uses ~300MB (200MB binary + ~100MB WebExtensions).
# With 4 uvicorn workers, default 1 per worker = 4 browsers total (~1.2GB,
# leaves ~1.8GB for HTTP + orchestrator). Override via CRAWLUTRON_BROWSER_POOL_SIZE.
DEFAULT_BROWSER_POOL_SIZE: int = 1


# --- Universal non-content hosts (hard-skip) ---
# Hosts that never carry crawlable municipal content: tracking/ad/social-feed
# infrastructure. Advertised links to them are still RECORDED on pages, but
# they are never enqueued as crawl jobs (any strategy) and the browser route
# handler aborts their requests unconditionally.
ANALYTICS_HOST_SUFFIXES = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googletagservices.com",
        "adservice.google.com",
        "facebook.net",
        "connect.facebook.net",
        "analytics.twitter.com",
        "x.com",
        "pinterest.com",
        "hotjar.com",
        "scorecardresearch.com",
        "mixpanel.com",
        "amplitude.com",
        "segment.io",
        "segment.com",
        "criteo.com",
        "pubmatic.com",
        "rubiconproject.com",
        "adroll.com",
    }
)

# Precomputed forms for the is_analytics_host / is_document_url hot paths
# (both run once per discovered link, up to thousands per page).
# ``is_analytics_host`` is true iff the host *is* a suffix (apex) or ends with
# "." + a suffix (subdomain). The apex-membership test uses the set directly;
# the per-subdomain dotted suffixes are precomputed to a tuple so ``endswith``
# takes a single C-level call instead of allocating ``"." + s`` per suffix.
_ANALYTICS_DOTTED = tuple("." + s for s in ANALYTICS_HOST_SUFFIXES)


def is_analytics_host(url: str) -> bool:
    """True when ``url`` points at a hard-skip host (apex or any subdomain)."""
    try:
        host = (_urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ANALYTICS_HOST_SUFFIXES or host.endswith(_ANALYTICS_DOTTED)


# --- Non-HTML resource extensions ---
# Documents yield no further HTML links, so URL-discovery crawls record their
# URLs but skip downloading them. Callers requesting EXTRACTION_DOCUMENT opt
# back in to fetching file contents.
DOCUMENT_URL_EXTENSIONS = frozenset(
    {".pdf", ".csv", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rtf"}
)

# Precomputed extension tuple for the is_document_url hot path: ``str.endswith``
# accepts a tuple and does a single C-level pass instead of re-building the
# tuple from the frozenset on every call.
_DOCUMENT_URL_EXTENSIONS = tuple(sorted(DOCUMENT_URL_EXTENSIONS))


def is_document_url(url: str) -> bool:
    """True when ``url``'s path ends with a known document extension."""
    try:
        path = _urlparse(url).path.lower()
    except Exception:
        return False
    return path.endswith(_DOCUMENT_URL_EXTENSIONS)
