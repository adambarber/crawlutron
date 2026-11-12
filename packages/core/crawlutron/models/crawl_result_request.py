from __future__ import annotations

from pydantic import BaseModel, Field


class CrawlResultRequest(BaseModel):
    """Request body for the stored-crawl read tools (MCP ``read_crawl_result`` /
    ``read_crawl_trace``), mirroring the GET path parameter of the REST replay doors."""

    request_id: str = Field(..., min_length=1, description="Request identifier of the stored crawl.")
