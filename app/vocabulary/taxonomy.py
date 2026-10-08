"""The vocabulary a crawl result is written in.

One concept declared as a set: what a page turned out to be
(:class:`CrawlClassification`), what that means for the caller's request
(:data:`ResultStatus` and the classification-to-status table), and whether it is
worth another pass (the transience sets). They live together because the tables
map over the enum: split them and every reader opens three files to answer "what
does this classification do to my result".

The static type aliases (``Classification``, ``CrawlError``, ``CrawlStrategyName``)
are in ``app/models/taxonomy.py``; that module is the type view of this one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

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
    # (``app.crawl.result_helpers.classify_error_code``). They are members of
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
    # (``classify_error_code``) and by the engine's unsupported-strategy
    # guard. Every one reaches the wire through the exception chokepoint
    # (``engine_result_from_exception`` / ``crawl_result_from_exception``),
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
    # Policy refusals from ``NormalizedUrl`` admission (unsupported scheme,
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
