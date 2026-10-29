from __future__ import annotations

from pydantic import BaseModel, Field

from crawlutron.models.unified_crawl_result import UnifiedCrawlResult


class UnifiedCrawlResponse(BaseModel):
    request_id: str = Field(description="Service-generated request identifier.")
    submitted_count: int = Field(description="Number of URLs accepted in the request payload.")
    policy_schema_version: str = Field(description="Policy schema version used for this execution.")
    success: bool = Field(description="True if the request was processed without fatal service errors.")
    results: list[UnifiedCrawlResult] = Field(description="Per-URL crawl results in submitted order.")
