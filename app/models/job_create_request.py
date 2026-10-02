from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.api_models import CrawlPolicy


class JobCreateRequest(BaseModel):
    """Create a new resumable whole-site crawl job."""

    model_config = ConfigDict(extra="forbid")

    root_url: str = Field(..., description="Root public HTTP(S) URL; stored canonically at crawl depth 0.")
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Crawl policy (strategies, limits, scope_regex, extraction).",
    )
