from __future__ import annotations

from pydantic import BaseModel, Field


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
