from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_strategy import CrawlStrategy
from app.models.crawl_timing import CrawlTiming
from app.models.mcp_capabilities import McpCapabilities
from app.models.search_capabilities import SearchCapabilities
from app.vocabulary.taxonomy import CrawlClassification


class CapabilitiesPayload(TypedDict):
    """The raw capabilities record, before pydantic validation.

    Names the shape the endpoint builds so a dropped key fails the build.
    Enum members arrive as their string values; nested maps are open JSON.
    """

    service: str
    version: str
    policy_schema_version: str
    supported_strategies: list[str]
    supported_extractions: list[str]
    supported_classifications: list[str]
    output_fields: list[str]
    default_limits: dict[str, object]
    features: dict[str, bool]
    search: dict[str, object]
    mcp: dict[str, object]
    crawl_timing: dict[str, object]
    openapi_url: str
    swagger_ui_url: str


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
