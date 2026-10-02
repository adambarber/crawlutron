from __future__ import annotations

from pydantic import BaseModel

from app.api_models import CrawlExtractionMethod, CrawlStrategy
from app.constants import CrawlClassification
from app.models.crawl_timing import CrawlTiming
from app.models.mcp_capabilities import McpCapabilities
from app.models.search_capabilities import SearchCapabilities


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
