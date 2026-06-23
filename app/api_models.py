from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


POLICY_SCHEMA_VERSION = "2026-06-23"
API_VERSION = "0.2.0"
SERVICE_NAME = "browser-crawler"


class CrawlStrategy(str, Enum):
    """Supported crawl execution strategies in fallback order."""

    HTTP_DECLARED_CRAWLER = "HTTP_DECLARED_CRAWLER"
    HTTP_SITE_COMPAT = "HTTP_SITE_COMPAT"
    BROWSER_HEADLESS = "BROWSER_HEADLESS"
    BROWSER_HEADED_WARMED = "BROWSER_HEADED_WARMED"
    DOCUMENT_EXTRACT = "DOCUMENT_EXTRACT"
    TRAFILATURA_EXTRACT = "TRAFILATURA_EXTRACT"


class CrawlClassification(str, Enum):
    """Normalized result classifications used across HTTP, browser, and document strategies."""

    rendered = "rendered"
    captcha_detected = "captcha_detected"
    bot_challenge_detected = "bot_challenge_detected"
    access_denied = "access_denied"
    login_required = "login_required"
    paywall_detected = "paywall_detected"
    rate_limited = "rate_limited"
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


class CrawlStatus(str, Enum):
    success = "success"
    blocked = "blocked"
    disallowed = "disallowed"
    retry_exhausted = "retry_exhausted"
    failed = "failed"


class CrawlPolicyLimits(BaseModel):
    timeout_seconds: int = Field(30, ge=1, le=120, description="Per-strategy timeout budget in seconds.")
    max_retries: int = Field(2, ge=0, le=5, description="Retry count for transient failures inside a selected strategy.")
    challenge_wait_seconds: int = Field(15, ge=0, le=120, description="Browser challenge wait budget for headed/warmed browser flows.")


class CrawlPolicy(BaseModel):
    schema_version: str = Field(POLICY_SCHEMA_VERSION, description="Caller-resolved policy schema version.")
    strategies: list[CrawlStrategy] = Field(
        default_factory=lambda: [CrawlStrategy.HTTP_DECLARED_CRAWLER, CrawlStrategy.BROWSER_HEADLESS],
        min_length=1,
        description="Ordered execution chain. The service records every attempted strategy and stops at the first successful content result.",
    )
    limits: CrawlPolicyLimits = Field(default_factory=CrawlPolicyLimits, description="Runtime limits applied to the execution plan.")
    headers: dict[str, str] = Field(default_factory=dict, description="Optional caller-approved headers for direct HTTP strategies.")


class CrawlRequest(BaseModel):
    url: str | None = Field(None, description="Single public HTTP(S) URL to crawl. Use either url or urls.")
    urls: list[str] | None = Field(None, description="Batch of public HTTP(S) URLs to crawl. Use either url or urls.")
    policy: CrawlPolicy = Field(default_factory=CrawlPolicy, description="Resolved caller-provided policy/execution plan.")


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


class CrawlResultModel(BaseModel):
    original_url: str = Field(description="Caller-provided URL before normalization.")
    normalized_url: str | None = Field(description="Public normalized URL used for execution, or null when disallowed.")
    final_url: str | None = Field(description="Final URL after redirects or browser navigation.")
    status: CrawlStatus = Field(description="Overall per-URL status.")
    classification: CrawlClassification = Field(description="Overall per-URL classification after fallback selection.")
    selected_strategy: CrawlStrategy | None = Field(description="Strategy that produced the selected result, or null when no strategy succeeded.")
    attempted_strategies: list[StrategyAttempt] = Field(description="Ordered per-strategy attempt history.")
    http_status: int | None = Field(None, description="Selected HTTP status, when available.")
    headers: dict[str, str] = Field(default_factory=dict, description="Selected response headers, lower-cased when observed.")
    content_type: str | None = Field(None, description="Selected content type, when available.")
    html: str | None = Field(None, description="Raw HTML/body evidence when requested and available.")
    text: str | None = Field(None, description="Extracted text evidence when available.")
    extraction_method: str | None = Field(None, description="Extraction method such as http_html, dom_text, pdf_text, or trafilatura_extract.")
    quality_score: float = Field(0.0, ge=0.0, le=1.0, description="Heuristic content quality score. HTTP 200 alone is not content success.")
    discovered_links: list[DiscoveredLink] = Field(default_factory=list, description="Structured links extracted from the selected HTML or rendered DOM. The service reports links but does not enqueue them recursively.")
    error: str | None = Field(None, description="Stable machine-readable error detail for the selected result, if any.")


class CrawlResponse(BaseModel):
    request_id: str = Field(description="Service-generated request identifier.")
    submitted_count: int = Field(description="Number of URLs accepted in the request payload.")
    policy_schema_version: str = Field(description="Policy schema version used for this execution.")
    results: list[CrawlResultModel] = Field(description="Per-URL crawl results in submitted order.")


class CrawlerError(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error summary.")
    details: dict[str, Any] = Field(default_factory=dict, description="Optional structured error details.")


class CapabilitiesResponse(BaseModel):
    service: str
    version: str
    policy_schema_version: str
    supported_strategies: list[CrawlStrategy]
    supported_classifications: list[CrawlClassification]
    output_fields: list[str]
    default_limits: dict[str, int]
    features: dict[str, bool]
    openapi_url: str
    swagger_ui_url: str


class AgentHintsResponse(BaseModel):
    service: str
    strategies: dict[CrawlStrategy, str]
    guidance: list[str]
    example_policies: dict[str, dict[str, Any]]


class DiscoveryResponse(BaseModel):
    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
