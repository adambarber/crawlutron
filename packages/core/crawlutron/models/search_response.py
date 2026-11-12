from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from crawlutron.models.search_result_item import SearchResultItem


class SearchResponse(BaseModel):
    """Response for ``POST /v1/search`` (LLM-friendly envelope)."""

    success: Annotated[
        bool,
        Field(
            description="True when the provider responded and results were shaped.",
        ),
    ] = True
    query: str = Field(description="Echo of the submitted query.")
    provider: str = Field(description="Search provider that produced the results.")
    limit: int = Field(description="Number of results returned.")
    data: list[SearchResultItem] = Field(default_factory=list, description="Ordered SERP results.")
