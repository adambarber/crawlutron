from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, Field

from crawlutron.models.classification_evidence import ClassificationEvidence
from crawlutron.models.content_validators import ContentValidators
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.discovered_link_response import DiscoveredLinkResponse
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.page_metadata_response import PageMetadataResponse
from crawlutron.models.strategy_attempt import StrategyAttempt
from crawlutron.vocabulary.taxonomy import CrawlClassification


class UnifiedCrawlResult(BaseModel):
    # 1. Identity & Routing
    original_url: str = Field(description="Caller-provided URL before normalization.")
    normalized_url: Annotated[
        NormalizedUrl | None,
        Field(
            description="Public normalized URL used for execution.",
        ),
    ] = None
    final_url: Annotated[
        NormalizedUrl | None,
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
    metadata: PageMetadataResponse = Field(
        default_factory=PageMetadataResponse,
        description="Extracted OpenGraph, Twitter, and JSON-LD metadata.",
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
    headers: Mapping[str, str] = Field(default_factory=dict)
    content_type: str | None = None
    extraction_method: Annotated[
        str | None,
        Field(
            description="http_html, dom_text, pdf_text, trafilatura_extract, etc.",
        ),
    ] = None
    # Open by design: browser worker metadata passes through; the internal
    # record (CrawlDiagnostics) carries the typed read surface.
    diagnostics: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="robots, sitemaps, frames, forms, js_endpoints, intercepted_xhr, etc.",
    )
    discovered_link_count: Annotated[int, Field(ge=0)] = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLinkResponse] = Field(default_factory=list)
    next_page_url: Annotated[
        NormalizedUrl | None,
        Field(description="Auto-detected pagination URL, if found."),
    ] = None
    validators: Annotated[
        ContentValidators | None,
        Field(
            description="Origin validators (ETag / Last-Modified) for the delivered representation. Echo "
            "these back via CrawlRequest.validators to revalidate cheaply; a 304 yields a not_modified "
            "result.",
        ),
    ] = None
