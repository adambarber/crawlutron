from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel

from app.models.auth_capabilities_record import AuthCapabilitiesRecord
from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_strategy import CrawlStrategy
from app.models.crawl_timing import CrawlTiming
from app.models.crawl_timing_record import CrawlTimingRecord
from app.models.mcp_capabilities import McpCapabilities
from app.models.mcp_capabilities_record import McpCapabilitiesRecord
from app.models.search_capabilities import SearchCapabilities
from app.models.search_capabilities_record import SearchCapabilitiesRecord
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
    default_limits: dict[str, int | float]
    features: dict[str, bool]
    auth: AuthCapabilitiesRecord
    search: SearchCapabilitiesRecord
    mcp: McpCapabilitiesRecord
    crawl_timing: CrawlTimingRecord
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
    auth: dict[str, str]
    crawl_timing: CrawlTiming
    search: SearchCapabilities
    mcp: McpCapabilities
    openapi_url: str
    swagger_ui_url: str
