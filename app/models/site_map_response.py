from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


class SiteMapResponse(BaseModel):
    """Response for ``POST /v1/map``."""

    url: Annotated[
        str | None,
        Field(
            description="Root URL (final_url or original_url) of the mapping crawl.",
        ),
    ] = None
    links: list[str] = Field(
        default_factory=list, description="In-page discovered links (sorted, de-duplicated)."
    )
    sitemaps: list[str] = Field(
        default_factory=list, description="robots.txt + sitemap.xml URLs (sorted, de-duplicated)."
    )
    feeds: list[str] = Field(
        default_factory=list, description="Discovered feed URLs (sorted, de-duplicated)."
    )
    all_urls: list[str] = Field(
        default_factory=list, description="De-duplicated union in stable encounter order."
    )
