"""One page as an extraction record: what a caller asked a crawl for."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.models.crawl_status import CrawlStatus
from app.models.normalized_url import NormalizedUrl
from app.vocabulary.taxonomy import CrawlClassification


class ExtractedPage(BaseModel):
    """A crawled page reduced to its content and its verdict.

    The projection exists because the full response carries the crawl's
    PROVENANCE -- attempts, headers, diagnostics, evidence, discovered links --
    and an MCP result carries its payload twice (text plus structured content).
    A caller who wants many pages' content pays for all of that per page. What
    stays here is the content and the verdict, and ``markdown`` is None when the
    page produced none: a blocked page reports its block rather than an empty
    string that reads like a successful extraction.
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
