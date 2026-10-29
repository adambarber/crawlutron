from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


class SearchResultItem(BaseModel):
    """A single SERP result from ``POST /v1/search``."""

    url: str = Field(
        description="Result URL, normalized to a public HTTP(S) URL. Private/loopback targets are dropped by "
        "the service."
    )
    title: str = Field(description="Result title from the search engine.")
    description: Annotated[str, Field(description="Result snippet from the search engine.")] = ""
