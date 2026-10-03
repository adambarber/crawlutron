from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.constants import CrawlClassification
from app.models.classification_evidence import ClassificationEvidence
from app.models.content_validators import ContentValidators
from app.models.crawl_status import CrawlStatus
from app.models.crawl_strategy import CrawlStrategy
from app.models.discovered_link_response import DiscoveredLinkResponse
from app.models.page_metadata_response import PageMetadataResponse
from app.models.strategy_attempt import StrategyAttempt


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
    discovered_links: list[DiscoveredLinkResponse] = Field(default_factory=list)
    next_page_url: Annotated[str | None, Field(description="Auto-detected pagination URL, if found.")] = None
    validators: Annotated[
        ContentValidators | None,
        Field(
            description="Origin validators (ETag / Last-Modified) for the delivered representation. Echo "
            "these back via CrawlRequest.validators to revalidate cheaply; a 304 yields a not_modified "
            "result.",
        ),
    ] = None
