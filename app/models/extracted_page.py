"""One page as an extraction record: what a caller asked a crawl for."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.models.crawl_status import CrawlStatus
from app.models.normalized_url import NormalizedUrl
from app.vocabulary.taxonomy import CrawlClassification


class ExtractedPage(BaseModel):
    """A crawled page reduced to its content and its verdict.

        The full response carries provenance (attempts, headers, evidence, links); this keeps
    only content + verdict, and ``markdown`` is None when the page produced none.
    """

    url: Annotated[
        NormalizedUrl | None,
        Field(description="Canonical URL of the page, or null for a URL admission refused."),
    ] = None
    status: CrawlStatus = Field(description="Page-level status.")
    classification: CrawlClassification = Field(description="Page-level classification.")
    title: Annotated[
        str | None,
        Field(description="Page title, when one was found."),
    ] = None
    markdown: Annotated[
        str | None,
        Field(description="Extracted markdown, when the page yielded any."),
    ] = None
