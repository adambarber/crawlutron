from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.constants import (
    DEFAULT_STRATEGIES,
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
    SUPPORTED_EXTRACTIONS,
    SUPPORTED_STRATEGIES,
)


POLICY_SCHEMA_VERSION = "2026-06-23"
API_VERSION = "0.2.0"
SERVICE_NAME = "browser-crawler"


class CrawlStrategy(str, Enum):
    """Fetch execution strategies, tried in fallback order.

    These describe *how* to retrieve bytes for a URL. How those bytes are
    parsed into text/markdown is a separate concern handled by
    :class:`CrawlExtractionMethod` (the ``extraction`` policy field), which is
    orthogonal to the fetch strategy.
    """

    HTTP_DECLARED_CRAWLER = HTTP_DECLARED_CRAWLER
    HTTP_CFFI_IMPERSONATE = HTTP_CFFI_IMPERSONATE
    BROWSER_HEADLESS = BROWSER_HEADLESS
    BROWSER_HEADED_WARMED = BROWSER_HEADED_WARMED


class CrawlExtractionMethod(str, Enum):
    """Output/parsing pipeline applied to content a fetch strategy retrieves.

    Extraction is orthogonal to the fetch strategy: any fetch strategy may be
    paired with any extraction method. ``FULL`` is the default unified
    pipeline (trafilatura HTML main text, markitdown for documents, plain-text
    passthrough). ``TRAFILATURA`` forces HTML main-text extraction only.
    ``DOCUMENT`` forces markitdown document extraction for PDFs and other
    non-HTML bodies.
    """

    FULL = EXTRACTION_FULL
    TRAFILATURA = EXTRACTION_TRAFILATURA
    DOCUMENT = EXTRACTION_DOCUMENT
    DOM = EXTRACTION_DOM


class CrawlClassification(str, Enum):
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
    tls_error = "tls_error"
    dns_error = "dns_error"
    invalid_url = "invalid_url"
    unsupported_scheme = "unsupported_scheme"
    private_network_target = "private_network_target"
    unknown_error = "unknown_error"
    not_modified = "not_modified"


class CrawlStatus(str, Enum):
    success = "success"
    blocked = "blocked"
    disallowed = "disallowed"
    retry_exhausted = "retry_exhausted"
    failed = "failed"
    skipped = "skipped"


class CrawlPolicyLimits(BaseModel):
    timeout_seconds: int = Field(30, ge=1, le=120, description="Per-strategy timeout budget in seconds. Default: 30. Applies separately to each attempted HTTP, document, or browser strategy.")
    max_retries: int = Field(2, ge=0, le=5, description="Retry count for transient failures inside a selected strategy. Default: 2. Retries do not bypass explicit denial, CAPTCHA, WAF, login, or paywall classifications.")
    challenge_wait_seconds: int = Field(15, ge=0, le=120, description="Passive browser challenge observation budget in seconds for browser flows. Default: 15. The service waits for public content to settle but does not click, solve, outsource, evade, rotate identities, or inject credentials.")
    max_discovered_links: int = Field(250, ge=0, le=2000, description="Bounded maximum structured discovered links returned per selected page. Default: 250. Link counts still report total discovered before truncation; callers own frontier queueing, dedupe, persistence, and recursion.")
    respect_robots: bool = Field(True, description="When True (default), /robots.txt rules are honored before fetching and for recursive-link filtering. Set False only for domains you are authorized to crawl without robots constraints.")
    max_depth: int = Field(0, ge=0, le=20, description="Bounded recursive crawl depth. 0 disables recursion (single-page crawl). When > 0 the engine follows in-scope discovered links up to max_pages total.")
    max_pages: int = Field(0, ge=0, le=2000, description="Bounded total pages for recursive crawls. 0 disables the cap (bounded only by max_depth).")
    max_rps: float = Field(0.0, ge=0.0, le=100.0, description="Maximum requests per second per domain enforced via token bucket. 0 disables rate limiting (default). When > 0, allows short bursts up to 5 tokens while sustaining the configured RPS.")
    max_content_length_bytes: int = Field(50 * 1024 * 1024, ge=0, description="Maximum content size in bytes per response. 50MB default protects against OOM from large PDFs or infinite JSON streams.")



class CrawlPolicy(BaseModel):
    schema_version: str = Field(POLICY_SCHEMA_VERSION, description="Caller-resolved policy schema version. Use the current policy_schema_version from /v1/capabilities or /v1/agent-hints when constructing durable policy templates.")
    strategies: list[CrawlStrategy] = Field(
        default_factory=lambda: [
            CrawlStrategy.HTTP_DECLARED_CRAWLER,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
            CrawlStrategy.BROWSER_HEADLESS,
            CrawlStrategy.BROWSER_HEADED_WARMED,
        ],
        min_length=1,
        description="Ordered fetch execution chain. Default: HTTP_DECLARED_CRAWLER, then HTTP_CFFI_IMPERSONATE, then BROWSER_HEADLESS, then BROWSER_HEADED_WARMED. Strategies run in order, the service records every attempted strategy, and fallback stops at the first successful content result. Available fetch strategies: HTTP_DECLARED_CRAWLER (transparent direct HTTP with the service's declared crawler identity), HTTP_CFFI_IMPERSONATE (TLS-fingerprint-impersonated HTTP via curl_cffi with browser-like headers for WAF/Cloudflare bypass), BROWSER_HEADLESS (headless Firefox rendering via Camoufox), BROWSER_HEADED_WARMED (headed Firefox with warmup URLs).",
    )
    extraction: CrawlExtractionMethod = Field(
        CrawlExtractionMethod.FULL,
        description="Output/parsing pipeline applied to the content the selected fetch strategy retrieves. Orthogonal to the fetch strategy. FULL (default) uses the unified pipeline: trafilatura HTML main text, markitdown for documents, plain-text passthrough. TRAFILATURA forces HTML main-text extraction only. DOCUMENT forces markitdown document extraction for PDFs and other non-HTML bodies. These are not strategies; pair any of them with any fetch strategy.",
    )
    limits: CrawlPolicyLimits = Field(default_factory=CrawlPolicyLimits, description="Runtime limits applied to the execution plan. Omit to use service defaults from /v1/capabilities; set per request when a host needs a tighter timeout, retry budget, passive challenge wait, or discovered-link cap.")
    headers: dict[str, str] = Field(default_factory=dict, description="Optional caller-approved headers for direct HTTP strategies. Use for explicit, reviewable host policies; do not use to smuggle credentials or bypass access controls.")
    request_profile: dict[str, Any] = Field(default_factory=dict, description="Optional direct HTTP compatibility profile for HTTP_CFFI_IMPERSONATE. Supported keys include user_agent_family (for example desktop_chrome), accept_language, fetch metadata hints, cookie_jar, and benign per-host cookies for public sessions. Applied only to direct HTTP strategies and echoed in diagnostics.")
    browser_profile: dict[str, Any] = Field(default_factory=dict, description="Optional browser state profile for browser strategies. Supported keys include state (fresh), viewport (desktop or mobile), resource_mode (full or reduced), and benign cookie jar hints. Viewport changes Firefox window size; reduced resource mode disables images.")
    discovery: dict[str, bool] = Field(default_factory=dict, description="Optional discovery toggles: robots, sitemaps, and feeds. Discovery output is diagnostic and non-recursive; the service reports discovered links and metadata but callers own frontier queueing, dedupe, and persistence.")
    scope_regex: str | None = Field(None, description="Optional regex applied to each discovered link URL to filter out-of-scope links before returning. Links whose normalized URL does not match are dropped from discovered_links.")
    css_selector: str | None = Field(None, description="Optional CSS selector. When provided, extraction targets only the matched DOM subtree (BeautifulSoup) and returns it as Markdown, bypassing full-page Trafilatura/markdownify.")
    xpath: str | None = Field(None, description="Optional XPath expression. When provided, extraction targets only the matched DOM nodes (lxml) and returns them as Markdown, bypassing full-page Trafilatura/markdownify.")


class CrawlRequest(BaseModel):
    url: str | None = Field(None, description="Single public HTTP(S) URL to crawl. Use either url or urls.")
    urls: list[str] | None = Field(None, description="Batch of public HTTP(S) URLs to crawl. Use either url or urls.")
    policy: CrawlPolicy = Field(default_factory=CrawlPolicy, description="Resolved caller-provided policy/execution plan.")
    webhook_url: str | None = Field(None, description="Optional callback URL. When provided, the crawl is accepted (202) and the UnifiedCrawlResponse is POSTed to this URL when complete.")


class ClassificationEvidence(BaseModel):
    classification: str = Field(description="Classification selected by this matched evidence.")
    signature: str = Field(description="Signature that matched the selected source bucket.")
    source: str = Field(description="Evidence source bucket, such as visible_text, html_without_script_style, or http_status.")
    context: str = Field("", description="Short normalized context around the matched signature.")


class DiscoveredLink(BaseModel):
    url: str = Field(description="Raw href value observed on the selected page.")
    normalized_url: str = Field(description="Absolute public HTTP(S) URL resolved against the selected page final URL.")
    text: str = Field("", description="Visible anchor text after whitespace normalization.")
    rel: list[str] = Field(default_factory=list, description="Anchor rel tokens, when present.")
    source_url: str = Field(description="Final page URL where this link was discovered.")
    host: str = Field(description="Host for the normalized URL.")
    is_same_host: bool = Field(description="True when the discovered link host matches the source page host exactly.")


class StrategyAttempt(BaseModel):
    strategy: CrawlStrategy = Field(description="Strategy attempted for this URL.")
    status: CrawlStatus = Field(description="Attempt-level status.")
    classification: CrawlClassification = Field(description="Attempt-level classification.")
    http_status: int | None = Field(None, description="HTTP status observed by this strategy, when available.")
    content_type: str | None = Field(None, description="Observed content type, when available.")
    elapsed_ms: int = Field(description="Attempt elapsed time in milliseconds.")
    error: str | None = Field(None, description="Stable machine-readable error detail for this attempt, if any.")


class FirecrawlMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    language: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    og_url: str | None = None
    og_type: str | None = None
    og_site_name: str | None = None
    twitter_card: str | None = None
    twitter_title: str | None = None
    twitter_description: str | None = None
    twitter_image: str | None = None
    canonical_url: str | None = None
    favicon: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    scrape_id: str | None = None
    source_url: str | None = None
    json_ld: dict | list | None = None


class UnifiedCrawlResult(BaseModel):
    # 1. Identity & Routing
    original_url: str = Field(description="Caller-provided URL before normalization.")
    normalized_url: str | None = Field(None, description="Public normalized URL used for execution.")
    final_url: str | None = Field(None, description="Final URL after redirects or browser navigation.")
    # 2. Execution Status
    status: CrawlStatus = Field(description="Overall per-URL status (success, blocked, failed, etc.).")
    classification: CrawlClassification = Field(description="Overall per-URL classification.")
    selected_strategy: CrawlStrategy | None = Field(None, description="Strategy that produced the selected result.")
    attempted_strategies: list[StrategyAttempt] = Field(default_factory=list, description="Ordered per-strategy attempt history.")
    error: str | None = Field(None, description="Stable machine-readable error detail, if any.")
    # 3. Firecrawl-Style LLM Output (The "Meat")
    markdown: str | None = Field(None, description="Cleaned page content in Markdown format for LLM consumption.")
    metadata: FirecrawlMetadata = Field(default_factory=FirecrawlMetadata, description="Extracted OpenGraph, Twitter, and JSON-LD metadata.")
    # 4. Raw Data & Evidence (For debugging & non-LLM pipelines)
    html: str | None = Field(None, description="Raw HTML/body evidence when requested and available.")
    text: str | None = Field(None, description="Extracted text evidence (pre-markdown) when available.")
    quality_score: float = Field(0.0, ge=0.0, le=1.0, description="Heuristic content quality score.")
    classification_evidence: list[ClassificationEvidence] = Field(default_factory=list)
    # 5. HTTP Details
    http_status: int | None = Field(None)
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = Field(None)
    extraction_method: str | None = Field(None, description="http_html, dom_text, pdf_text, trafilatura_extract, etc.")
    # 6. Discovery & Diagnostics
    diagnostics: dict[str, Any] = Field(default_factory=dict, description="robots, sitemaps, frames, forms, js_endpoints, intercepted_xhr, etc.")
    discovered_link_count: int = Field(0, ge=0)
    discovered_links_truncated: bool = Field(False)
    discovered_links: list[DiscoveredLink] = Field(default_factory=list)
    next_page_url: str | None = Field(None, description="Auto-detected pagination URL, if found.")


class UnifiedCrawlResponse(BaseModel):
    request_id: str = Field(description="Service-generated request identifier.")
    submitted_count: int = Field(description="Number of URLs accepted in the request payload.")
    policy_schema_version: str = Field(description="Policy schema version used for this execution.")
    success: bool = Field(description="True if the request was processed without fatal service errors.")
    results: list[UnifiedCrawlResult] = Field(description="Per-URL crawl results in submitted order.")


class CrawlerError(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error summary.")
    details: dict[str, Any] = Field(default_factory=dict, description="Optional structured error details.")


class ReadinessResponse(BaseModel):
    status: str = Field(description="Capacity state: ready below saturation, saturated at configured active crawl capacity.")
    active_requests: int = Field(description="Currently active /v1/crawl and /v1/render requests tracked by the service.")
    max_active_requests: int = Field(description="Configured active request capacity, derived from global crawler concurrency.")
    saturated: bool = Field(description="True when active crawl work is at capacity; callers should treat HTTP 503 as backpressure, not process death.")


class CapabilitiesResponse(BaseModel):
    service: str
    version: str
    policy_schema_version: str
    supported_strategies: list[CrawlStrategy]
    supported_extractions: list[CrawlExtractionMethod]
    supported_classifications: list[CrawlClassification]
    output_fields: list[str]
    default_limits: dict[str, int]
    features: dict[str, bool]
    openapi_url: str
    swagger_ui_url: str


class AgentHintsResponse(BaseModel):
    service: str
    strategies: dict[CrawlStrategy, str]
    extractions: dict[CrawlExtractionMethod, str]
    guidance: list[str]
    example_policies: dict[str, dict[str, Any]]


class DiscoveryResponse(BaseModel):
    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
