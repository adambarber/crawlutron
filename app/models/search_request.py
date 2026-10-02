from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    """Request body for ``POST /v1/search``.

    A web-search query that returns SERP-style results (title, url,
    description) from the configured provider. The service does not crawl
    result URLs; callers pipe them into ``POST /v1/crawl`` themselves.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500, description="Web search query.")
    limit: Annotated[
        int | None,
        Field(
            ge=1,
            le=100,
            description="Maximum results to return. Defaults to the server setting "
            "CRAWLUTRON_SEARCH_LIMIT_DEFAULT and is clamped to "
            "CRAWLUTRON_SEARCH_LIMIT_MAX.",
        ),
    ] = None
