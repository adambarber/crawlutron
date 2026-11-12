from __future__ import annotations

from pydantic import BaseModel

from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.example_policy import ExamplePolicy


class AgentHintsResponse(BaseModel):
    service: str
    strategies: dict[CrawlStrategy, str]
    extractions: dict[CrawlExtractionMethod, str]
    guidance: list[str]
    example_policies: dict[str, ExamplePolicy]
