"""Single source of truth for policy/strategy/extraction magic strings.

These literals are consumed by ``app.api_models``,
``app.discovery`` and the test suite so that spelling drift (or a stray
non-ASCII look-alike character) cannot desynchronize comparisons. Always
import the constant instead of typing the string literal.
"""

from __future__ import annotations

from urllib.parse import urlparse as _urlparse

# ---------------------------------------------------------------------------
# Fetch strategies (how bytes are retrieved). Orthogonal to extraction.
# ---------------------------------------------------------------------------
HTTP_DECLARED_CRAWLER: str = "HTTP_DECLARED_CRAWLER"
HTTP_CFFI_IMPERSONATE: str = "HTTP_CFFI_IMPERSONATE"
BROWSER_HEADLESS: str = "BROWSER_HEADLESS"

# Service version: single source of truth (L2). app.api_models.API_VERSION
# aliases this so existing importers keep working.
SERVICE_VERSION: str = "0.3.0"
# Public-facing identity strings. Kept here so the same UA appears in both the
# declared-crawler header and the synthetic ``User-Agent:`` line we attach
# to outbound requests, and so the package version is sourced from a single
# place instead of being duplicated across files.
# Overridable via BROWSER_CRAWLER_USER_AGENT; update before publishing
# to the final GitHub org (replace the placeholder URL below).
DEFAULT_USER_AGENT: str = (
    f"browser-crawler/{SERVICE_VERSION} (+https://github.com/browser-crawler/browser-crawler)"
)
USER_AGENT_TOKEN: str = "browser-crawler"
# TLS-fingerprint target for the HTTP_CFFI_IMPERSONATE strategy (L2): the
# fetcher and the agent-hints copy both reference this, so they cannot drift.
CFFI_IMPERSONATE_TARGET: str = "firefox135"
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
# Hard ceiling a caller may set for a single page's body cap. Above it an
# explicit ``max_content_length_bytes`` would disable the concurrency-scaled
# RAM budget and inflate the decompression bound — so it is rejected, not
# honored (matches max_rps). Set to the default total RAM budget: one page
# may never outgrow the whole in-flight allowance.
MAX_CONTENT_LENGTH_BYTES: int = 1 * 1024 * 1024 * 1024
DISCOVERY_BODY_MAX_BYTES: int = 8 * 1024 * 1024

# Per-strategy timeout / retry budgets.
DEFAULT_HTTP_TIMEOUT_SECONDS: int = 30
DEFAULT_MAX_RETRIES: int = 2
MAX_RETRIES: int = 5

# Hard per-URL ceiling over the ENTIRE strategy chain (all strategies x retries
# plus browser challenge waits). Guards against poisoned/tarpit URLs that would
# otherwise hold a concurrency slot for many minutes while the resource gate
# reports idle capacity. Generous so it never trips on healthy targets.
DEFAULT_TOTAL_URL_BUDGET_SECONDS: float = 180.0
MAX_TOTAL_URL_BUDGET_SECONDS: float = 900.0
# Upper bound for the per-domain token-bucket rate limiter (CrawlPolicyLimits
# max_rps). Mirrors the API model's le=100.0 bound so the normalized policy
# path and the pydantic path reject the same values.
MAX_RATE_LIMIT_RPS: float = 100.0

# Cache TTLs (seconds).
DEFAULT_CACHE_TTL_SECONDS: float = 3600.0

# Disk content-cache byte budget (H9). max_entries bounds entry count, not
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
SITEMAP_CACHE_EVICT_BATCH: int = 200
SITEMAP_FANOUT_TIMEOUT_SECONDS: int = 45
# Maximum child-sitemap fetches in flight per index level (H7). An index
# lists every child before any max_urls accounting, so without chunking a
# single hostile sitemapindex could spawn tens of thousands of tasks.
SITEMAP_INDEX_FANOUT: int = 16

# Engine scheduling: a recursive-crawl batch pulls global_concurrency * this.
ENGINE_BATCH_CONCURRENCY_MULTIPLIER: int = 2

# Browser runtime limits (also exposed as CrawlerSettings fields with
# BROWSER_CRAWLER_* env mappings).
DEFAULT_MAX_BROWSER_RENDERS: int = 100
DEFAULT_MAX_CONCURRENT_PAGES: int = 3

# Browser pool: number of persistent browser instances to keep warm PER WORKER.
# Each browser uses ~300MB (200MB binary + ~100MB WebExtensions).
# With 4 uvicorn workers, default 1 per worker = 4 browsers total (~1.2GB,
# leaves ~1.8GB for HTTP + orchestrator). Override via BROWSER_CRAWLER_BROWSER_POOL_SIZE.
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
