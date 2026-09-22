from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants import (
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
    MAX_CONTENT_LENGTH_BYTES,
    MAX_DISCOVERED_LINKS,
    MAX_RETRIES,
    MAX_TOTAL_URL_BUDGET_SECONDS,
    CrawlClassification,
)

POLICY_SCHEMA_VERSION = "2026-06-23"


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


class CrawlStatus(str, Enum):
    success = "success"
    blocked = "blocked"
    disallowed = "disallowed"
    retry_exhausted = "retry_exhausted"
    failed = "failed"
    skipped = "skipped"


class DiscoveryConfig(BaseModel):
    """Typed discovery toggles for crawl policy.

    Only recognized keys are accepted; extra keys are rejected.
    """

    robots: bool = Field(
        True,
        description="Honor /robots.txt rules and report robots crawl-delay, sitemaps, and disallow patterns.",
    )
    sitemaps: bool = Field(
        True,
        description="Fetch and parse sitemap.xml / sitemap_index.xml URLs from robots.txt and common locations.",
    )
    feeds: bool = Field(
        True, description='Detect RSS/Atom/JSON Feed links via <link rel="alternate"> and common feed paths.'
    )

    model_config = ConfigDict(extra="forbid")


class FetchValidators(BaseModel):
    """Caller-supplied cached validators for cheap conditional revalidation.

    Supply the ETag / Last-Modified you received from a previous crawl of the
    same URL; matching URLs are revalidated with If-None-Match /
    If-Modified-Since instead of fully re-fetched. A 304 yields a
    ``not_modified`` result (no content) so freshness monitors avoid paying
    full-fetch cost on unchanged pages.
    """

    if_none_match: str | None = Field(
        None, description="ETag from a previous crawl result; sent as If-None-Match."
    )
    if_modified_since: str | None = Field(
        None, description="Last-Modified date from a previous crawl result; sent as If-Modified-Since."
    )


class ContentValidators(BaseModel):
    """Validators returned by the origin for the delivered representation.

    Feed ``etag`` / ``last_modified`` back as ``if_none_match`` /
    ``if_modified_since`` on a later request to revalidate cheaply.
    """

    etag: str | None = Field(None, description="Origin ETag for the final representation, if provided.")
    last_modified: str | None = Field(None, description="Origin Last-Modified date, if provided.")


class CrawlPolicyLimits(BaseModel):
    timeout_seconds: int = Field(
        DEFAULT_HTTP_TIMEOUT_SECONDS,
        ge=1,
        le=120,
        description="Per-strategy timeout budget in seconds. Default: 30. Applies separately to each attempted HTTP, document, or browser strategy.",
    )
    max_retries: int = Field(
        DEFAULT_MAX_RETRIES,
        ge=0,
        le=MAX_RETRIES,
        description="Retry count for transient failures inside a selected strategy. Default: 2. Retries do not bypass explicit denial, CAPTCHA, WAF, login, or paywall classifications.",
    )
    challenge_wait_seconds: int = Field(
        DEFAULT_CHALLENGE_WAIT_SECONDS,
        ge=0,
        le=120,
        description="Passive browser challenge observation budget in seconds for browser flows. Default: 15. The service waits for public content to settle but does not click, solve, outsource, evade, rotate identities, or inject credentials.",
    )
    max_discovered_links: int = Field(
        DEFAULT_MAX_DISCOVERED_LINKS,
        ge=0,
        le=MAX_DISCOVERED_LINKS,
        description="Bounded maximum structured discovered links returned per selected page. Default: 100 (reduced from 250 to cap per-page result memory under parallel crawls). Link counts still report total discovered before truncation; callers own cross-run frontier queueing, dedupe, persistence, and recursion (the service collapses URLs that normalize to the same page within a request).",
    )
    respect_robots: bool = Field(
        True,
        description="When True (default), /robots.txt rules are honored before fetching and for recursive-link filtering. Set False only for domains you are authorized to crawl without robots constraints.",
    )
    max_depth: int = Field(
        0,
        ge=0,
        le=20,
        description="Bounded recursive crawl depth. 0 disables recursion (single-page crawl). When > 0 the engine follows in-scope discovered links up to max_pages total.",
    )
    max_pages: int = Field(
        0,
        ge=0,
        le=2000,
        description="Bounded total pages for recursive crawls. 0 disables the cap (bounded only by max_depth).",
    )
    max_rps: float = Field(
        0.0,
        ge=0.0,
        le=100.0,
        description="Maximum requests per second per domain enforced via token bucket. 0 disables rate limiting (default). When > 0, allows short bursts up to 5 tokens while sustaining the configured RPS.",
    )
    max_content_length_bytes: int | None = Field(
        None,
        ge=0,
        le=MAX_CONTENT_LENGTH_BYTES,
        description="Maximum content size in bytes per response. Omit (recommended) to let the service scale the per-page cap against its RAM budget and concurrency; set an explicit value only when a host needs a hard cap regardless of parallelism. Capped at MAX_CONTENT_LENGTH_BYTES.",
    )
    total_url_budget_seconds: float = Field(
        DEFAULT_TOTAL_URL_BUDGET_SECONDS,
        ge=0.1,
        le=MAX_TOTAL_URL_BUDGET_SECONDS,
        description="Hard per-URL deadline across the whole strategy chain (seconds). A poisoned/tarpit URL that exceeds this is failed as network_error instead of holding a concurrency slot. Default: 180 (the floor of 0.1 only prevents accidental near-zero values; production configs should keep this generous).",
    )


class CrawlPolicy(BaseModel):
    # Rejected policy.headers names -> why (M12). Conditional pairs keep the
    # M14 reason (they must arrive via ``validators``); the rest bypass an
    # engine chokepoint or spoof origin-side identity. The map is the single
    # ban list -- adding an entry here is the whole change surface.
    _BANNED_HEADERS: ClassVar[dict[str, str]] = {
        "if-none-match": "conditional headers belong to the validators map",
        "if-modified-since": "conditional headers belong to the validators map",
        "cookie": "the cookie jar owns Cookie; a raw header bypasses the cookie-namespace chokepoint",
        "host": "Host is owned by the pinned-request chokepoint and is never forwarded verbatim",
        "proxy-authorization": "proxy credentials are not a crawl input",
        "x-forwarded-for": "origin-side identity headers must not be caller-controlled",
        "x-forwarded-host": "origin-side identity headers must not be caller-controlled",
        "x-real-ip": "origin-side identity headers must not be caller-controlled",
    }

    schema_version: str = Field(
        POLICY_SCHEMA_VERSION,
        description="Caller-resolved policy schema version. Use the current policy_schema_version from /v1/capabilities or /v1/agent-hints when constructing durable policy templates.",
    )
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
    limits: CrawlPolicyLimits = Field(
        default_factory=CrawlPolicyLimits,
        description="Runtime limits applied to the execution plan. Omit to use service defaults from /v1/capabilities; set per request when a host needs a tighter timeout, retry budget, passive challenge wait, or discovered-link cap.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Optional caller-approved headers for direct HTTP strategies. Use for explicit, reviewable host policies; do not use to smuggle credentials or bypass access controls.",
    )
    request_profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional direct HTTP compatibility profile for HTTP_CFFI_IMPERSONATE. Supported keys include user_agent_family (for example desktop_chrome), accept_language, fetch metadata hints, cookie_jar, and benign per-host cookies for public sessions. Applied only to direct HTTP strategies and echoed in diagnostics.",
    )
    browser_profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional browser state profile for browser strategies. Supported keys include state (fresh), viewport (desktop or mobile), resource_mode (full or reduced), benign cookie jar hints, and auto_accept_cookie_consent (default false; when true the browser may click consent banners). Viewport changes Firefox window size; reduced resource mode disables images.",
    )
    discovery: DiscoveryConfig | None = Field(
        None,
        description="Optional discovery configuration. Only robots, sitemaps, and feeds are recognized. Omit to use defaults (all enabled).",
    )
    scope_regex: str | None = Field(
        None,
        description="Optional regex applied to each discovered link URL to filter out-of-scope links before returning. Links whose normalized URL does not match are dropped from discovered_links.",
    )
    css_selector: str | None = Field(
        None,
        description="Optional CSS selector. When provided, extraction targets only the matched DOM subtree (BeautifulSoup) and returns it as Markdown, bypassing full-page Trafilatura/markdownify.",
    )
    xpath: str | None = Field(
        None,
        description="Optional XPath expression. When provided, extraction targets only the matched DOM nodes (lxml) and returns them as Markdown, bypassing full-page Trafilatura/markdownify.",
    )

    @field_validator("scope_regex")
    @classmethod
    def _validate_scope_regex(cls, value: str | None) -> str | None:
        from app.safe_regex import UnsafePattern, compile_untrusted

        if value is None:
            return None
        try:
            compile_untrusted(value, max_len=256)
        except UnsafePattern as exc:
            raise ValueError(f"scope_regex rejected: {exc}") from exc
        return value

    @field_validator("browser_profile")
    @classmethod
    def _reject_execute_js_actions(cls, profile: dict[str, Any]) -> dict[str, Any]:
        """Reject caller-supplied ``execute_js`` actions (#2, RCE-class risk).

        The browser action chain never runs arbitrary JavaScript, and we also
        block it at the API boundary so a malicious payload never reaches a
        render. Benign profile keys (viewport, resource_mode, include_screenshot,
        screenshot_selectors, consent hints, etc.) are unaffected.
        """
        actions = (profile or {}).get("actions") or []
        for action in actions:
            if isinstance(action, dict) and str(action.get("type", "")).lower() == "execute_js":
                raise ValueError("execute_js actions are not permitted in browser_profile")
        return profile

    @model_validator(mode="after")
    def _reject_banned_headers(self) -> CrawlPolicy:
        """Reject chokepoint-bypassing header names in ``policy.headers`` (M12).

        Anchored on ``CrawlPolicy`` (not a request model) so every endpoint
        that embeds a policy -- crawl, map, render alike -- inherits the ban
        from one site; a per-request validator is a per-call-site guard the
        engine's chokepoints would still have to re-trust. Rejection, never a
        silent strip, so the caller learns the contract (#1912).

        ``authorization`` is deliberately absent: credentialed crawling of
        one's own site is a real use case, and the typed replacement
        (``policy.credentials``) does not exist yet. Banning the header
        before the replacement would only push callers into baking
        credentials into URLs (worse). It lands here when Tier 2b does.
        """
        for key in self.headers:
            reason = self._BANNED_HEADERS.get(key.strip().lower())
            if reason is not None:
                raise ValueError(f"header '{key}' is not permitted in policy.headers: {reason}")
        return self


class _UrlSource(Protocol):
    """Contract shared by every request model that accepts exactly one of ``url``/``urls``."""

    url: str | None
    urls: list[str] | None


def _require_exactly_one_url_source(model: _UrlSource) -> _UrlSource:
    """Shared url/urls contract for crawl and render requests (L5, N4).

    Both set silently ignored ``url``; neither failed late in
    ``_payload_urls``. Exactly one source is required, and ``urls=[]`` is
    NOT "absent": it dumps through exclude_none and resolves to zero
    targets, so an identity check (not truthiness) gates the contract.
    """
    has_url = model.url is not None
    has_urls = model.urls is not None
    if has_url == has_urls:
        raise ValueError("supply exactly one of 'url' or 'urls'")
    if has_urls and not model.urls:
        raise ValueError("'urls' must contain at least one URL")
    return model


class CrawlRequest(BaseModel):
    url: str | None = Field(None, description="Single public HTTP(S) URL to crawl. Use either url or urls.")
    urls: list[str] | None = Field(
        None, description="Batch of public HTTP(S) URLs to crawl. Use either url or urls."
    )
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy, description="Resolved caller-provided policy/execution plan."
    )
    validators: dict[str, FetchValidators] = Field(
        default_factory=dict,
        description="Optional map of submitted URL -> cached validators for conditional revalidation. "
        "Keys may be the original or normalized URL. Matching URLs are fetched with "
        "If-None-Match / If-Modified-Since; a 304 produces a not_modified result with no content.",
    )
    async_mode: bool = Field(
        False,
        description="When true, accept immediately (202) and run the crawl in the background. Results are persisted and available at /v1/crawl/{request_id}. If webhook_url is also provided, the result is delivered there on completion.",
    )
    webhook_url: str | None = Field(
        None,
        description="Optional callback URL. When provided with async_mode, the crawl is accepted (202) and the UnifiedCrawlResponse is POSTed to this URL when complete.",
    )
    output_format: str | None = Field(
        None,
        description="#17 Multiple output formats for the crawl results. One of: json (default), jsonl, ndjson, markdown, csv. Non-json formats are returned as a plain Response with the matching media type instead of a UnifiedCrawlResponse model.",
    )

    @model_validator(mode="after")
    def _require_url_source(self):
        return _require_exactly_one_url_source(self)


class MapRequest(BaseModel):
    """#2 Site-map request: discover the URL frontier for a root URL.

    Discovery (robots sitemaps + feeds + in-page links) is forced on; the caller
    controls the fetch ``policy`` (strategies, limits, scope_regex) and the rest
    is delegated to the shared crawl engine.
    """

    model_config = ConfigDict(extra="ignore")

    url: str = Field(..., description="Root public HTTP(S) URL to map.")
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Optional crawl policy for the mapping crawl (strategies, limits, scope_regex). Discovery is always enabled.",
    )


class SiteMapResponse(BaseModel):
    """Response for ``POST /v1/map``."""

    url: str | None = Field(None, description="Root URL (final_url or original_url) of the mapping crawl.")
    links: list[str] = Field(
        default_factory=list, description="In-page discovered links (sorted, de-duplicated)."
    )
    sitemaps: list[str] = Field(
        default_factory=list, description="robots.txt + sitemap.xml URLs (sorted, de-duplicated)."
    )
    feeds: list[str] = Field(
        default_factory=list, description="Discovered feed URLs (sorted, de-duplicated)."
    )
    all_urls: list[str] = Field(
        default_factory=list, description="De-duplicated union in stable encounter order."
    )


class SearchRequest(BaseModel):
    """Request body for ``POST /v1/search``.

    A web-search query that returns SERP-style results (title, url,
    description) from the configured provider. The service does not crawl
    result URLs; callers pipe them into ``POST /v1/crawl`` themselves.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500, description="Web search query.")
    limit: int | None = Field(
        None,
        ge=1,
        le=100,
        description="Maximum results to return. Defaults to the server setting "
        "BROWSER_CRAWLER_SEARCH_LIMIT_DEFAULT and is clamped to "
        "BROWSER_CRAWLER_SEARCH_LIMIT_MAX.",
    )


class SearchResultItem(BaseModel):
    """A single SERP result from ``POST /v1/search``."""

    url: str = Field(
        description="Result URL, normalized to a public HTTP(S) URL. Private/loopback targets are dropped by the service."
    )
    title: str = Field(description="Result title from the search engine.")
    description: str = Field("", description="Result snippet from the search engine.")


class SearchResponse(BaseModel):
    """Response for ``POST /v1/search`` (LLM-friendly envelope)."""

    success: bool = Field(True, description="True when the provider responded and results were shaped.")
    query: str = Field(description="Echo of the submitted query.")
    provider: str = Field(description="Search provider that produced the results.")
    limit: int = Field(description="Number of results returned.")
    data: list[SearchResultItem] = Field(default_factory=list, description="Ordered SERP results.")


class RenderOptionsRequest(BaseModel):
    """Browser render options for ``/v1/render`` (all optional; server applies defaults)."""

    model_config = ConfigDict(extra="ignore")

    timeout_seconds: int | None = Field(None, description="Per-render navigation timeout in seconds.")
    max_retries: int | None = Field(None, description="Maximum retries for a transient render failure.")
    respect_robots: bool | None = Field(
        None, description="Whether to honor robots.txt for the render target."
    )
    include_html: bool | None = Field(None, description="Include the rendered HTML in the result.")
    include_text: bool | None = Field(None, description="Include extracted visible text in the result.")


class RenderRequest(BaseModel):
    """Request body for ``POST /v1/render`` (backward-compatible browser render)."""

    model_config = ConfigDict(extra="ignore")

    url: str | None = Field(None, description="Single public HTTP(S) URL to render. Use either url or urls.")
    urls: list[str] | None = Field(
        None, description="Batch of public HTTP(S) URLs to render. Use either url or urls."
    )
    options: RenderOptionsRequest | None = Field(
        None, description="Browser render options; omitted fields use server defaults."
    )

    @model_validator(mode="after")
    def _require_url_source(self):
        return _require_exactly_one_url_source(self)


class ClassificationEvidence(BaseModel):
    classification: str = Field(description="Classification selected by this matched evidence.")
    signature: str = Field(description="Signature that matched the selected source bucket.")
    source: str = Field(
        description="Evidence source bucket, such as extracted_text, html_without_scripts_styles, or http_status."
    )
    context: str = Field("", description="Short normalized context around the matched signature.")


class DiscoveredLink(BaseModel):
    url: str = Field(description="Raw href value observed on the selected page.")
    normalized_url: str = Field(
        description="Absolute public HTTP(S) URL resolved against the selected page final URL."
    )
    text: str = Field("", description="Visible anchor text after whitespace normalization.")
    rel: list[str] = Field(default_factory=list, description="Anchor rel tokens, when present.")
    source_url: str = Field(description="Final page URL where this link was discovered.")
    host: str = Field(description="Host for the normalized URL.")
    is_same_host: bool = Field(
        description="True when the discovered link host matches the source page host exactly."
    )


class StrategyAttempt(BaseModel):
    # Optional: an envelope built after a worker raised before any strategy ran
    # records None ("no strategy attempted") rather than fabricating a member.
    strategy: CrawlStrategy | None = Field(description="Strategy attempted for this URL, or null when none was.")
    status: CrawlStatus = Field(description="Attempt-level status.")
    classification: CrawlClassification = Field(description="Attempt-level classification.")
    http_status: int | None = Field(
        None, description="HTTP status observed by this strategy, when available."
    )
    content_type: str | None = Field(None, description="Observed content type, when available.")
    elapsed_ms: int = Field(description="Attempt elapsed time in milliseconds.")
    error: str | None = Field(
        None, description="Stable machine-readable error detail for this attempt, if any."
    )


class PageMetadata(BaseModel):
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
    selected_strategy: CrawlStrategy | None = Field(
        None, description="Strategy that produced the selected result."
    )
    attempted_strategies: list[StrategyAttempt] = Field(
        default_factory=list, description="Ordered per-strategy attempt history."
    )
    error: str | None = Field(None, description="Stable machine-readable error detail, if any.")
    # 3. LLM Output (The "Meat")
    markdown: str | None = Field(
        None, description="Cleaned page content in Markdown format for LLM consumption."
    )
    metadata: PageMetadata = Field(
        default_factory=PageMetadata, description="Extracted OpenGraph, Twitter, and JSON-LD metadata."
    )
    # 4. Raw Data & Evidence (For debugging & non-LLM pipelines)
    html: str | None = Field(None, description="Raw HTML/body evidence when requested and available.")
    text: str | None = Field(None, description="Extracted text evidence (pre-markdown) when available.")
    quality_score: float = Field(0.0, ge=0.0, le=1.0, description="Heuristic content quality score.")
    classification_evidence: list[ClassificationEvidence] = Field(default_factory=list)
    # 5. HTTP Details
    http_status: int | None = Field(None)
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = Field(None)
    extraction_method: str | None = Field(
        None, description="http_html, dom_text, pdf_text, trafilatura_extract, etc."
    )
    # 6. Discovery & Diagnostics
    diagnostics: dict[str, Any] = Field(
        default_factory=dict,
        description="robots, sitemaps, frames, forms, js_endpoints, intercepted_xhr, etc.",
    )
    discovered_link_count: int = Field(0, ge=0)
    discovered_links_truncated: bool = Field(False)
    discovered_links: list[DiscoveredLink] = Field(default_factory=list)
    next_page_url: str | None = Field(None, description="Auto-detected pagination URL, if found.")
    validators: ContentValidators | None = Field(
        None,
        description="Origin validators (ETag / Last-Modified) for the delivered representation. Echo these back via CrawlRequest.validators to revalidate cheaply; a 304 yields a not_modified result.",
    )


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
    status: str = Field(
        description="Capacity state: ready below the self-managed resource ceiling, saturated when memory or CPU utilization rides at the gate target."
    )
    active_requests: int = Field(
        description="Currently active /v1/crawl and /v1/render requests tracked by the service."
    )
    max_active_requests: int = Field(
        description="0 = unbounded; saturation is resource-driven (memory/CPU utilization at the self-managed gate target), not a fixed request cap."
    )
    saturated: bool = Field(
        description="True when the self-managed resource guard is at capacity; callers should treat HTTP 503 as backpressure, not process death."
    )


class SearchCapabilities(BaseModel):
    """Declared web-search capability for ``/v1/capabilities``."""

    active_provider: str = Field(
        description="Provider ``auto`` selection resolves to for the current settings."
    )
    configured_providers: list[str] = Field(
        description="Providers with credentials configured, in priority order."
    )
    endpoint: str = Field(description="The web-search endpoint path.")
    provider_selection: str = Field(description="Human-readable summary of how the provider is selected.")


class McpCapabilities(BaseModel):
    """Declared MCP capability for ``/v1/capabilities``."""

    endpoint: str = Field(description="The MCP streamable-HTTP endpoint path.")
    protocol_versions: list[str] = Field(description="MCP protocol versions the server implements.")


class CrawlTiming(BaseModel):
    """Latency contract advertised for ``/v1/crawl`` on ``/v1/capabilities``."""

    per_url_budget_seconds: int = Field(
        description="Wall-clock budget for a single URL's whole strategy chain; a URL over it is failed, not the crawl."
    )
    sync_has_server_time_ceiling: bool = Field(
        description="Whether a synchronous POST /v1/crawl is capped by a server-side wall clock (it is not)."
    )
    large_crawl_guidance: str = Field(
        description="How to avoid a long-blocking synchronous response (async_mode / webhook_url / POST /v1/jobs)."
    )


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
    crawl_timing: CrawlTiming
    search: SearchCapabilities
    mcp: McpCapabilities
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


class JobCreateRequest(BaseModel):
    """Create a new resumable whole-site crawl job (#3+#14)."""

    model_config = ConfigDict(extra="ignore")

    root_url: str = Field(..., description="Root public HTTP(S) URL to crawl recursively.")
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy, description="Crawl policy (strategies, limits, scope_regex, extraction)."
    )


class JobCreateResponse(BaseModel):
    """Response from creating a crawl job."""

    model_config = ConfigDict(extra="ignore")

    job_id: str = Field(..., description="Unique job identifier.")
    status: str = Field(..., description="Initial job status (always 'pending').")
    root_url: str
    created_at: float


class JobStatusResponse(BaseModel):
    """Status of a crawl job."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    root_url: str
    status: str
    created_at: float
    updated_at: float
    frontier_size: int
    visited_size: int
    results_count: int
    stats: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class JobListResponse(BaseModel):
    """List of crawl jobs."""

    model_config = ConfigDict(extra="ignore")

    jobs: list[JobStatusResponse]


class JobControlRequest(BaseModel):
    """Control a crawl job (pause/resume/cancel)."""

    model_config = ConfigDict(extra="ignore")

    action: str = Field(..., description="One of: pause, resume, cancel.")
