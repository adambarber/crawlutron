from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_strategy import CrawlStrategy


class AgentHintsPayload(TypedDict):
    """The raw agent-hints record, before pydantic validation."""

    service: str
    strategies: dict[str, str]
    extractions: dict[str, str]
    guidance: list[str]
    example_policies: dict[str, object]


class AgentHintsResponse(BaseModel):
    service: str
    strategies: dict[CrawlStrategy, str]
    extractions: dict[CrawlExtractionMethod, str]
    guidance: list[str]
    example_policies: dict[str, dict[str, Any]]
