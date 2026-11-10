"""Every number a crawl is bounded by: defaults and hard caps.

Policy limits, extraction deadlines, sitemap ceilings, cache sizes and browser
ceilings; a number an operator may raise holds its default and cap side by side.
"""

from __future__ import annotations

from typing import Final

MAX_SCREENSHOT_SELECTORS: Final[int] = 16

# Most entries in one policy's ordered fetch-strategy ladder; the vocabulary is
# three strategies and cookie replay needs a repeat or two after a browser render.
MAX_CRAWL_STRATEGIES: Final[int] = 16

#: Most scroll steps in one lazy-load pass. Deep pages settle in a few dozen;
#: this is well past the point of usefulness, not the point of cost.
MAX_LAZY_SCROLL_STEPS: Final[int] = 50

#: Longest pause between scroll steps, in seconds.
MAX_LAZY_SCROLL_DELAY_SECONDS: Final[float] = 5.0

#: Longest wait for a session-cookie harvest: a finally-block hold on a
#: pooled renderer, not the caller's own budget.
MAX_COOKIE_HARVEST_TIMEOUT_SECONDS: Final[float] = 5.0

#: Longest wait for one page.evaluate: Playwright's evaluate has NO default
#: deadline, so a wedged renderer would hold the pooled page forever.
MAX_EVALUATE_TIMEOUT_SECONDS: Final[float] = 5.0

# Discovered links returned per selected page.
DEFAULT_MAX_DISCOVERED_LINKS: int = 100
# Hard cap on the field AND the default for the extreme /v1/map whole-site
# discovery path (intentionally the maximum, not the conservative 100).
MAX_DISCOVERED_LINKS: int = 2000

# Job-list pagination: limit held for the GET /v1/jobs slice. `offset` is
# caller-chosen and unbounded; only the page keeps a default and a cap.
DEFAULT_JOB_LIST_LIMIT: Final[int] = 20
MAX_JOB_LIST_LIMIT: Final[int] = 100

# Passive browser challenge observation budget (seconds).
DEFAULT_CHALLENGE_WAIT_SECONDS: int = 15

# Per-response body size bound.
DEFAULT_MAX_CONTENT_LENGTH_BYTES: int = 10 * 1024 * 1024
# Hard ceiling for a page's body cap: above it an explicit cap would disable
# the concurrency-scaled RAM budget; one page may never outgrow the allowance.
MAX_CONTENT_LENGTH_BYTES: int = 1 * 1024 * 1024 * 1024
DISCOVERY_BODY_MAX_BYTES: int = 8 * 1024 * 1024

# Per-strategy timeout / retry budgets.
DEFAULT_HTTP_TIMEOUT_SECONDS: int = 30
# The caller's per-strategy timeout ceiling: the model's `le` and the
# deployment default's bound are one number (the pair that used to drift).
MAX_HTTP_TIMEOUT_SECONDS: int = 120

# Longest header VALUE a caller may set: at the common 8 KB interop
# ceiling origins accept, it also bounds one policy's total header size.
MAX_HEADER_VALUE_BYTES: Final[int] = 8 * 1024

# Three retry numbers, three authorities: DEFAULT (no operator stated one),
# MAX_RETRIES (deployment ceiling), MAX_JOB_MAX_RETRIES (one flaky job may exceed it).
DEFAULT_MAX_RETRIES: int = 5
MAX_RETRIES: int = 5
MAX_JOB_MAX_RETRIES: int = 10

# Durable whole-site retry cap: a transient failure re-queues (never marks
# visited) so an outage cannot drain a job; after this many, the URL is terminal.
MAX_URL_TRANSIENT_ATTEMPTS: int = 3

# Hard per-URL ceiling over the ENTIRE strategy chain: a tarpit URL must not
# hold a slot for minutes while the resource gate reports idle capacity.
DEFAULT_TOTAL_URL_BUDGET_SECONDS: float = 180.0
# The single source for large-crawl guidance; one constant feeds capabilities,
# agent-hints and the OpenAPI description so the advice cannot drift.
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
# Upper bound for the per-domain token-bucket rate limiter; the API model's
# `le=` reads this constant -- one source of truth for the bound.
MAX_RATE_LIMIT_RPS: float = 100.0

# Cache TTLs (seconds).
DEFAULT_CACHE_TTL_SECONDS: float = 3600.0

# Disk content-cache byte budget: max_entries bounds count, not size (one
# envelope can hold multi-MB); oversize entries are refused, LRU enforces total.
CONTENT_CACHE_MAX_ENTRY_BYTES: int = 4 * 1024 * 1024
CONTENT_CACHE_MAX_TOTAL_BYTES: int = 512 * 1024 * 1024

# Sitemap discovery tuning.
SITEMAP_MAX_DEPTH: int = 3
SITEMAP_MAX_URLS: int = 10_000
SITEMAP_CACHE_TTL_SECONDS: float = DEFAULT_CACHE_TTL_SECONDS
# A failure must not stand for the full hour (mirror ROBOTS_FAILURE_TTL_SECONDS).
SITEMAP_FAILURE_TTL_SECONDS: float = 60.0
SITEMAP_CACHE_MAX_ENTRIES: int = 1000
SITEMAP_FANOUT_TIMEOUT_SECONDS: int = 45
# Max child-sitemap fetches in flight per index level: without chunking, one
# hostile sitemapindex could spawn tens of thousands of tasks.
SITEMAP_INDEX_FANOUT: int = 16

# Engine scheduling: a recursive-crawl batch pulls global_concurrency * this.
ENGINE_BATCH_CONCURRENCY_MULTIPLIER: int = 2

# Browser runtime limits (also exposed as CrawlerSettings fields with
# CRAWLUTRON_* env mappings).
DEFAULT_MAX_BROWSER_RENDERS: int = 100
DEFAULT_MAX_CONCURRENT_PAGES: int = 3

# Browser fixed-overhead knobs (scroll steps, pause, harvest wait), equal to
# the long-standing hardcoded behavior; change via CrawlerSettings/env or profile.
DEFAULT_LAZY_SCROLL_STEPS: int = 3
DEFAULT_LAZY_SCROLL_DELAY_SECONDS = 1
DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS: float = 5.0

# Extraction deadlines: one document extraction, one selector evaluation, one
# worker acquisition. Change via CrawlerSettings/env, never by editing these.
DEFAULT_EXTRACTION_DOC_TIMEOUT: float = 10.0
DEFAULT_EXTRACTION_SELECTOR_TIMEOUT: float = 2.0
DEFAULT_EXTRACTION_DOC_ACQUIRE_TIMEOUT: float = 90.0

# Largest markdown one document may contribute: a pathological file otherwise
# sizes every link, content-hash and SQLite row downstream.
DEFAULT_EXTRACTION_MAX_MARKDOWN: int = 5 * 1024 * 1024

# Persistent browsers per worker fallback lives in derive_browser_pool_size
# (pressure_sizing.py): sizing is RAM-derived, not a fixed constant.
