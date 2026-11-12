"""The vocabulary a crawl result is written in.

One concept as a set -- what a page turned out to be, its status meaning, and
whether another pass is worth it; the type aliases live in models/taxonomy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

# The classification taxonomy: THE single source of truth -- every strategy's
# classification is a member, and the groupings derive from it, not re-list it.


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
    # The per-URL budget's own signal: alive but slower than the caller's budget
    # -- distinct from network_error (host answered) and navigation_timeout.
    url_budget_exceeded = "url_budget_exceeded"
    # Strategy failure codes (classify_error_code): members because they are
    # stored in CrawlStrategyAttempt.classification, where a non-member 422s.
    browser_pool_exhausted = "browser_pool_exhausted"
    # The breaker was open, so the URL was never attempted: a capacity decision,
    # not a dead page -- a member so the code survives the strict response enum.
    circuit_breaker_open = "circuit_breaker_open"
    # Admission never reached a renderer: the loop stalled on capacity, reason
    # unknown -- never dispatched, never a page verdict.
    admission_stalled = "admission_stalled"
    payload_too_large = "payload_too_large"
    too_many_redirects = "too_many_redirects"
    http_error = "http_error"
    value_error = "value_error"
    tls_error = "tls_error"
    dns_error = "dns_error"
    invalid_url = "invalid_url"
    unsupported_scheme = "unsupported_scheme"
    private_network_target = "private_network_target"
    # Live host-policy refusals: the deployment denied (or de-allowed) a host
    # while a watch still pointed at it; terminal, so the sweep records the reason.
    host_denied = "host_denied"
    host_not_allowed = "host_not_allowed"
    # The constructor refuses a URL carrying its own credentials (userinfo is
    # not a crawlable identity); a watch holding one is refused the same way.
    userinfo_refused = "userinfo_refused"
    unknown_error = "unknown_error"
    not_modified = "not_modified"


# Classification groupings: set operations over one another -- each member is
# named once, in its leaf grouping; deriving from the enum makes typos import errors.
def _classification_values(*members: CrawlClassification) -> frozenset[str]:
    """The label set for a leaf grouping; a non-leaf grouping is a ``|`` of earlier
    ones, so this is called with a member only where it is first classified, and
    a member is never spelled twice.
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

# Crawl result status (a URL's high-level outcome). CLASSIFICATION_STATUS_BY_LABEL
# maps every classification onto one of these; ``crawlutron.models`` re-exports the alias.
ResultStatus = Literal["success", "blocked", "disallowed", "retry_exhausted", "failed", "skipped"]

# What each status means for DOMAIN health: success/blocked keep the host
# healthy, failed/retry_exhausted fault it, skipped/disallowed say nothing.
DomainHealth = Literal["success", "failure", "none"]

DOMAIN_HEALTH_BY_STATUS: dict[ResultStatus, DomainHealth] = {
    "success": "success",
    "blocked": "success",
    "failed": "failure",
    "retry_exhausted": "failure",
    "skipped": "none",
    "disallowed": "none",
}

# Classification -> result status: THE table -- it replaced two if-chains that
# disagreed on unrecognized labels, and seeds future per-classification metadata.
CLASSIFICATION_STATUS_BY_LABEL: dict[str, ResultStatus] = {
    # Usable content, or freshness proved by the caller's own validators.
    "rendered": "success",
    "not_modified": "success",
    # Anti-bot / consent / access walls: the page exists, we were kept out.
    **dict.fromkeys(HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS, "blocked"),
    # Retrieved, but nothing a consumer can use.
    **dict.fromkeys(FAILED_CLASSIFICATIONS, "failed"),
    # Fetch-level failure codes are known-dead outcomes: they must not take the
    # "blocked" default, which would keep a dead page eligible for re-serve.
    "payload_too_large": "failed",
    "too_many_redirects": "failed",
    "http_error": "failed",
    "url_budget_exceeded": "failed",
    # Capacity, not a dead page: never attempted because no renderer was free.
    # "failed" + diagnostics["retryable"] -- the verdict is about the attempt.
    "browser_pool_exhausted": "failed",
    # Capacity, not a dead page: the domain's breaker was open, so the URL was
    # never attempted. Same retry contract as browser_pool_exhausted above.
    "circuit_breaker_open": "failed",
    # Capacity, not a dead page: admission stalled before a renderer was ever
    # assigned; TRANSIENT_CLASSIFICATIONS keeps it re-eligible.
    "admission_stalled": "failed",
    # Exception codes from the shared classifier and the engine's guard: all
    # reach the wire as terminal failures, so their status is "failed", not "blocked".
    "network_error": "failed",
    "connection_error": "failed",
    "tls_error": "failed",
    "dns_error": "failed",
    "http_status_error": "failed",
    "value_error": "failed",
    "browser_crash": "failed",
    "unknown_error": "failed",
    # A robots disallow is a deliberate non-attempt: "skipped" says we chose
    # not to fetch, keeping the URL out of failed-bookkeeping.
    "robots_disallowed": "skipped",
    # Policy refusals from admission (scheme, private target, malformed URL):
    # caller-visible refusals, never a page verdict.
    "invalid_url": "disallowed",
    "unsupported_scheme": "disallowed",
    "private_network_target": "disallowed",
    # Live host-policy refusals (#674): the same caller-visible refusal family.
    "host_denied": "disallowed",
    "host_not_allowed": "disallowed",
    "userinfo_refused": "disallowed",
    # The browser retry loop spent its attempts: the page may be renderable
    # later -- the verdict describes the attempt, not the page.
    "navigation_timeout": "retry_exhausted",
}

# Unknown-classification default "blocked": "success" would launder untriaged
# pages into pinning/caching and "failed" asserts they are dead; blocked is revisitable.
UNKNOWN_CLASSIFICATION_STATUS: ResultStatus = "blocked"


def status_for_classification(classification: str) -> ResultStatus:
    """Map a page classification onto the crawl result status.

    Every classification a strategy can emit is an explicit key in the table; only
    labels outside the taxonomy take :data:`UNKNOWN_CLASSIFICATION_STATUS`.
    """
    return CLASSIFICATION_STATUS_BY_LABEL.get(classification, UNKNOWN_CLASSIFICATION_STATUS)


# Classification transience: is a page that failed this way worth another pass?
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
        # Capacity, not a dead page: the status table already says failed; this
        # says the page stays eligible, which the frontier re-queue consumes.
        CrawlClassification.browser_pool_exhausted,
        CrawlClassification.circuit_breaker_open,
        CrawlClassification.admission_stalled,
        # A renderer died rather than a page.
        CrawlClassification.browser_crash,
        # Untriaged: the one case where another pass is pure upside.
        CrawlClassification.unknown_error,
    )
)
# NON_TRANSIENT is stated, not defaulted -- only the pair's union is the
# complete verdict the test derives from: members are origin-chosen or ours.
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
        CrawlClassification.host_denied,
        CrawlClassification.host_not_allowed,
        CrawlClassification.userinfo_refused,
    )
    # Not failures at all: a page that rendered, and a page the caller's own
    # validators proved fresh.
    | _classification_values(
        CrawlClassification.rendered,
        CrawlClassification.not_modified,
    )
)


def is_transient_classification(classification: str | None) -> bool:
    """Return whether ``classification`` deserves another pass; the single answer
    for every retry decision, failing closed and with ``None`` -- an untriaged
    label is not retried, the direction that costs politeness.
    """
    return classification in TRANSIENT_CLASSIFICATIONS


# Exhaustive: context-dependent statuses beyond the table default.
# ``unknown_error`` is the one: "failed" from the engine, "retry_exhausted" from the loop.
CONTEXT_VARYING_STATUS: dict[str, frozenset[ResultStatus]] = {
    "unknown_error": frozenset({"retry_exhausted"}),
}


def status_agrees_with_classification(classification: str, status: str) -> bool:
    """Whether a ``(classification, status)`` pair is one the taxonomy allows --
    the guard that makes a disagreeing pair unrepresentable at the result
    chokepoint: the status must be the table default or a context-dependent one.
    """
    if status == status_for_classification(classification):
        return True
    return status in CONTEXT_VARYING_STATUS.get(classification, frozenset())
