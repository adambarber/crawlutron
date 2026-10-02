from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_strategy import CrawlStrategy


class AgentHintsResponse(BaseModel):
    service: str
    strategies: dict[CrawlStrategy, str]
    extractions: dict[CrawlExtractionMethod, str]
    guidance: list[str]
    example_policies: dict[str, dict[str, Any]]
