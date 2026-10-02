from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.api_models import CrawlExtractionMethod, CrawlStrategy


class AgentHintsResponse(BaseModel):
    service: str
    strategies: dict[CrawlStrategy, str]
    extractions: dict[CrawlExtractionMethod, str]
    guidance: list[str]
    example_policies: dict[str, dict[str, Any]]
