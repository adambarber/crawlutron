"""Single source of truth for policy/strategy/extraction magic strings.

These literals are consumed by ``app.service``, ``app.api_models``,
``app.discovery`` and the test suite so that spelling drift (or a stray
non-ASCII look-alike character) cannot desynchronize comparisons. Always
import the constant instead of typing the string literal.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Fetch strategies (how bytes are retrieved). Orthogonal to extraction.
# ---------------------------------------------------------------------------
HTTP_DECLARED_CRAWLER: str = "HTTP_DECLARED_CRAWLER"
HTTP_CFFI_IMPERSONATE: str = "HTTP_CFFI_IMPERSONATE"
BROWSER_HEADLESS: str = "BROWSER_HEADLESS"
BROWSER_HEADED_WARMED: str = "BROWSER_HEADED_WARMED"

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
DEFAULT_EXTRACTION: str = EXTRACTION_FULL

# Human-readable descriptions for discovery/agent-hints.
EXTRACTION_DESCRIPTIONS: dict[str, str] = {
    EXTRACTION_FULL: "Unified output pipeline: trafilatura HTML main text, markitdown for documents, plain-text passthrough.",
    EXTRACTION_TRAFILATURA: "HTML-only main-text extraction for noisy HTML; does not parse PDF or binary documents.",
    EXTRACTION_DOCUMENT: "Document/body extraction via markitdown for PDFs and non-HTML responses when text evidence is needed.",
    EXTRACTION_DOM: "DOM-text extraction: rendered or parsed page text passthrough without the document or Trafilatura pipelines.",
}

# ---------------------------------------------------------------------------
# extraction_method result codes (diagnostic/provenance labels in crawl results).
# ---------------------------------------------------------------------------
EXTRACTION_METHOD_HTTP_HTML: str = "http_html"
EXTRACTION_METHOD_HTTP_BODY: str = "http_body"
EXTRACTION_METHOD_DOCUMENT: str = "document_extract"
EXTRACTION_METHOD_TRAFILATURA: str = "trafilatura_extract"
EXTRACTION_METHOD_DOM_TEXT: str = "dom_text"
EXTRACTION_METHOD_BINARY_NO_TEXT: str = "binary_no_text"


# ---------------------------------------------------------------------------
# Classification groupings (consumed by HTTP/browser strategy status mappers
# and the test suite). Centralized so the HTTP vs browser blocked sets cannot
# silently drift apart across modules.
# ---------------------------------------------------------------------------
SUCCESS_CLASSIFICATIONS: frozenset[str] = frozenset({"rendered"})

HTTP_BLOCKED_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
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
    }
)

BROWSER_BLOCKED_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "captcha_detected",
        "bot_challenge_detected",
        "access_denied",
        "login_required",
        "paywall_detected",
        "rate_limited",
    }
)


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
DISCOVERY_BODY_MAX_BYTES: int = 8 * 1024 * 1024

# Per-strategy timeout / retry budgets.
DEFAULT_HTTP_TIMEOUT_SECONDS: int = 30
DEFAULT_MAX_RETRIES: int = 2
MAX_RETRIES: int = 5

# Cache TTLs (seconds).
DEFAULT_CACHE_TTL_SECONDS: float = 3600.0

# Sitemap discovery tuning.
SITEMAP_MAX_DEPTH: int = 3
SITEMAP_MAX_URLS: int = 10_000
SITEMAP_CACHE_TTL_SECONDS: float = DEFAULT_CACHE_TTL_SECONDS
SITEMAP_CACHE_MAX_ENTRIES: int = 1000
SITEMAP_CACHE_EVICT_BATCH: int = 200
SITEMAP_FANOUT_TIMEOUT_SECONDS: int = 45

# Engine scheduling: a recursive-crawl batch pulls global_concurrency * this.
ENGINE_BATCH_CONCURRENCY_MULTIPLIER: int = 2

# Browser runtime limits (also exposed as CrawlerSettings fields with
# BROWSER_CRAWLER_* env mappings).
DEFAULT_MAX_BROWSER_RENDERS: int = 100
DEFAULT_MAX_CONCURRENT_PAGES: int = 3


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
        "cdc.gov",
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


def is_analytics_host(url: str) -> bool:
    """True when ``url`` points at a hard-skip host (apex or any subdomain)."""
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == s or host.endswith("." + s) for s in ANALYTICS_HOST_SUFFIXES)
