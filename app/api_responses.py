"""Response and capability models for the public wire surface.

Split out of ``app.api_models``, which keeps the caller-supplied side. The seam
IS that split: every model here describes what this service produced, and every
model left behind describes what a caller sent. The input-side ``extra="forbid"``
rule stops at this boundary because of it -- an extra key on a response is not a
caller misconfiguring anything.

Nothing here validates caller intent. That is not a stylistic preference but the
reason the split is safe to make: these models are the leaf of the wire contract
and are referenced by the endpoints, so moving them changes where a class lives
and nothing about what it accepts.

``from __future__ import annotations`` and the pydantic-mypy comment are
inherited from the module this was cut out of; see there for why the fail gate
needs ``plugins = pydantic.mypy`` to read any model in either file.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.api_models import (
    ContentValidators,
    CrawlExtractionMethod,
    CrawlStatus,
    CrawlStrategy,
)
from app.constants import CrawlClassification


class SiteMapResponse(BaseModel):
    """Response for ``POST /v1/map``."""

    url: Annotated[
        str | None,
        Field(
            description="Root URL (final_url or original_url) of the mapping crawl.",
        ),
    ] = None
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


class SearchResultItem(BaseModel):
    """A single SERP result from ``POST /v1/search``."""

    url: str = Field(
        description="Result URL, normalized to a public HTTP(S) URL. Private/loopback targets are dropped by "
        "the service."
    )
    title: str = Field(description="Result title from the search engine.")
    description: Annotated[str, Field(description="Result snippet from the search engine.")] = ""


class SearchResponse(BaseModel):
    """Response for ``POST /v1/search`` (LLM-friendly envelope)."""

    success: Annotated[
        bool,
        Field(
            description="True when the provider responded and results were shaped.",
        ),
    ] = True
    query: str = Field(description="Echo of the submitted query.")
    provider: str = Field(description="Search provider that produced the results.")
    limit: int = Field(description="Number of results returned.")
    data: list[SearchResultItem] = Field(default_factory=list, description="Ordered SERP results.")


class ClassificationEvidence(BaseModel):
    classification: str = Field(description="Classification selected by this matched evidence.")
    signature: str = Field(description="Signature that matched the selected source bucket.")
    source: str = Field(
        description="Evidence source bucket, such as extracted_text, html_without_scripts_styles, or "
        "http_status."
    )
    context: Annotated[
        str,
        Field(
            description="Short normalized context around the matched signature.",
        ),
    ] = ""


class DiscoveredLink(BaseModel):
    url: str = Field(description="Raw href value observed on the selected page.")
    normalized_url: str = Field(
        description="Absolute public HTTP(S) URL resolved against the selected page final URL."
    )
    text: Annotated[str, Field(description="Visible anchor text after whitespace normalization.")] = ""
    rel: list[str] = Field(default_factory=list, description="Anchor rel tokens, when present.")
    source_url: str = Field(description="Final page URL where this link was discovered.")
    host: str = Field(description="Host for the normalized URL.")
    is_same_host: bool = Field(
        description="True when the discovered link host matches the source page host exactly."
    )


class StrategyAttempt(BaseModel):
    # Optional: an envelope built after a worker raised before any strategy ran
    # records None ("no strategy attempted") rather than fabricating a member.
    strategy: CrawlStrategy | None = Field(
        description="Strategy attempted for this URL, or null when none was."
    )
    status: CrawlStatus = Field(description="Attempt-level status.")
    classification: CrawlClassification = Field(description="Attempt-level classification.")
    http_status: Annotated[
        int | None,
        Field(
            description="HTTP status observed by this strategy, when available.",
        ),
    ] = None
    content_type: Annotated[str | None, Field(description="Observed content type, when available.")] = None
    elapsed_ms: int = Field(description="Attempt elapsed time in milliseconds.")
    error: Annotated[
        str | None,
        Field(
            description="Stable machine-readable error detail for this attempt, if any.",
        ),
    ] = None


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
    normalized_url: Annotated[
        str | None,
        Field(
            description="Public normalized URL used for execution.",
        ),
    ] = None
    final_url: Annotated[
        str | None,
        Field(
            description="Final URL after redirects or browser navigation.",
        ),
    ] = None
    # 2. Execution Status
    status: CrawlStatus = Field(description="Overall per-URL status (success, blocked, failed, etc.).")
    classification: CrawlClassification = Field(description="Overall per-URL classification.")
    selected_strategy: Annotated[
        CrawlStrategy | None,
        Field(
            description="Strategy that produced the selected result.",
        ),
    ] = None
    attempted_strategies: list[StrategyAttempt] = Field(
        default_factory=list, description="Ordered per-strategy attempt history."
    )
    error: Annotated[str | None, Field(description="Stable machine-readable error detail, if any.")] = None
    # 3. LLM Output (The "Meat")
    markdown: Annotated[
        str | None,
        Field(
            description="Cleaned page content in Markdown format for LLM consumption.",
        ),
    ] = None
    metadata: PageMetadata = Field(
        default_factory=PageMetadata, description="Extracted OpenGraph, Twitter, and JSON-LD metadata."
    )
    # 4. Raw Data & Evidence (For debugging & non-LLM pipelines)
    html: Annotated[
        str | None,
        Field(
            description="Raw HTML/body evidence when requested and available.",
        ),
    ] = None
    text: Annotated[
        str | None,
        Field(
            description="Extracted text evidence (pre-markdown) when available.",
        ),
    ] = None
    quality_score: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Heuristic content quality score.",
        ),
    ] = 0.0
    classification_evidence: list[ClassificationEvidence] = Field(default_factory=list)
    # 5. HTTP Details
    http_status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = None
    extraction_method: Annotated[
        str | None,
        Field(
            description="http_html, dom_text, pdf_text, trafilatura_extract, etc.",
        ),
    ] = None
    # 6. Discovery & Diagnostics
    diagnostics: dict[str, Any] = Field(
        default_factory=dict,
        description="robots, sitemaps, frames, forms, js_endpoints, intercepted_xhr, etc.",
    )
    discovered_link_count: Annotated[int, Field(ge=0)] = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLink] = Field(default_factory=list)
    next_page_url: Annotated[str | None, Field(description="Auto-detected pagination URL, if found.")] = None
    validators: Annotated[
        ContentValidators | None,
        Field(
            description="Origin validators (ETag / Last-Modified) for the delivered representation. Echo "
            "these back via CrawlRequest.validators to revalidate cheaply; a 304 yields a not_modified "
            "result.",
        ),
    ] = None


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
        description="Capacity state: ready below the self-managed resource ceiling, saturated when memory or "
        "CPU utilization rides at the gate target."
    )
    active_requests: int = Field(
        description="Currently active /v1/crawl and /v1/render requests tracked by the service."
    )
    max_active_requests: int = Field(
        description="0 = unbounded; saturation is resource-driven (memory/CPU utilization at the "
        "self-managed "
        "gate target), not a fixed request cap."
    )
    saturated: bool = Field(
        description="True when the self-managed resource guard is at capacity; callers should treat HTTP 503 "
        "as backpressure, not process death."
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
        description="Wall-clock budget for a single URL's whole strategy chain; a URL over it is failed, not "
        "the crawl."
    )
    sync_has_server_time_ceiling: bool = Field(
        description="Whether a synchronous POST /v1/crawl is capped by a server-side wall clock (it is not)."
    )
    large_crawl_guidance: str = Field(
        description="How to avoid a long-blocking synchronous response (async_mode / webhook_url / POST "
        "/v1/jobs)."
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
    kind: Annotated[
        str,
        Field(
            description="Job kind: whole_site resumes from its persisted frontier; single_shot "
            "does not. They differ in whether a crash leaves the job recoverable, so "
            "neither can be inferred from the other.",
        ),
    ]
    created_at: float
    updated_at: float
    frontier_size: int
    visited_size: int
    results_count: int
    cap_truncated: Annotated[
        bool,
        Field(
            description="True only when a positive job max_pages cap refused or truncated eligible queued or "
            "discovered pages. Ordinary BATCH_SIZE chunking does not set it; already-admitted in-flight "
            "overcommit is reported separately in stats.cap_overcommitted.",
        ),
    ] = False
    stats: dict[str, int] = Field(
        default_factory=dict,
        description="Job counters; cap_truncated marks eligible queued/discovered work refused by a positive "
        "cap, while cap_overcommitted marks already-admitted in-flight work preserved beyond that cap.",
    )
    error: str | None = None


class JobListResponse(BaseModel):
    """List of crawl jobs."""

    model_config = ConfigDict(extra="ignore")

    jobs: list[JobStatusResponse]
