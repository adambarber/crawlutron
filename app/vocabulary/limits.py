"""Every number a crawl is bounded by: defaults and hard caps.

Policy limits (timeouts, retries, budgets, discovered links), the extraction
deadlines, the sitemap ceilings, the cache sizes and the browser-profile
ceilings. A number an operator may raise has its default here and its cap beside
it, because a default without a cap is a limit nothing enforces.
"""

from __future__ import annotations

from typing import Final

MAX_SCREENSHOT_SELECTORS: Final[int] = 16

# ---------------------------------------------------------------------------
# Ceilings on the caller-supplied knobs behind the closed browser-profile keys.
#
# A closed key set answers *which* keys exist. It is silent about how large their
# values may be, and four of these took whatever the caller sent. Each ceiling is
# placed where the code already said the value should have one: the scroll runs
# directly below an action chain that is explicitly budgeted and whose docstring
# says production must pass a budget, and the cookie harvest holds a *pooled*
# renderer from a finally block.
#
# These are enforced twice, on purpose. The model refuses so the caller gets a
# 422 naming the key; the readers clamp so a profile reaching a renderer by any
# route other than the model -- which is every route, since the profile is a
# dict by then -- is still bounded. The model is a boundary; the reader is the
# guarantee.
# ---------------------------------------------------------------------------

#: Most scroll steps in one lazy-load pass. Deep pages settle in a few dozen;
#: this is well past the point of usefulness, not the point of cost.
MAX_LAZY_SCROLL_STEPS: Final[int] = 50

#: Longest pause between scroll steps, in seconds.
MAX_LAZY_SCROLL_DELAY_SECONDS: Final[float] = 5.0

#: Longest wait for a session-cookie harvest, in seconds. The harvest runs in a
#: finally block against a pooled renderer, so this is a hold on a shared
#: resource rather than on the caller's own budget.
MAX_COOKIE_HARVEST_TIMEOUT_SECONDS: Final[float] = 5.0

# ---------------------------------------------------------------------------
# Crawl policy limit defaults and hard caps.
#
# Single source of truth for the numeric limits that previously lived as bare
# literals / inline os.environ reads across app/models, payload_helpers,
# http.py, main.py, discovery/* and browser.py. Change a limit HERE, not in N
# places. The pydantic Fields in app/models reference these so the advertised
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
# max_rps). The API model's `le=` reads this constant, so there is one source
# of truth for the bound.
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

# Browser fixed-overhead knobs (configurability only):
# lazy-load scroll repetitions, the pause between scrolls, and the bounded
# wait for the cookie harvest. Values equal the long-standing hardcoded
# behavior (3 steps / 1s / 5.0s); change them via CrawlerSettings/env or the
# per-request browser_profile, never by editing these defaults.
DEFAULT_LAZY_SCROLL_STEPS: int = 3
DEFAULT_LAZY_SCROLL_DELAY_SECONDS = 1
DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS: float = 5.0

# Extraction deadlines: how long one document extraction may run in its worker,
# how long one selector evaluation may block, and how long acquiring a free
# worker may wait before the crawl gives up on the document. Change them via
# CrawlerSettings/env, never by editing these defaults.
DEFAULT_EXTRACTION_DOC_TIMEOUT: float = 10.0
DEFAULT_EXTRACTION_SELECTOR_TIMEOUT: float = 2.0
DEFAULT_EXTRACTION_DOC_ACQUIRE_TIMEOUT: float = 90.0

# The largest markdown a single document may contribute, in characters. One
# pathological or adversarial file otherwise costs the whole downstream budget:
# every link, every content-hash and every SQLite row is sized by it.
DEFAULT_EXTRACTION_MAX_MARKDOWN: int = 5 * 1024 * 1024

# Browser pool: number of persistent browser instances to keep warm PER WORKER.
# Each browser uses ~300MB (200MB binary + ~100MB WebExtensions).
# With 4 uvicorn workers, default 1 per worker = 4 browsers total (~1.2GB,
# leaves ~1.8GB for HTTP + orchestrator). Override via CRAWLUTRON_BROWSER_POOL_SIZE.
DEFAULT_BROWSER_POOL_SIZE: int = 1
