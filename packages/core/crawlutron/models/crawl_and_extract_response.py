"""The ``crawl_and_extract`` composite's result: extraction records, in submitted order."""

from __future__ import annotations

from pydantic import BaseModel, Field

from crawlutron.models.extracted_page import ExtractedPage


class CrawlAndExtractResponse(BaseModel):
    """The crawl response read as extraction records.

    ``submitted_count`` sits beside the pages so a caller can tell a deduplicated
    result (several URLs normalized to one page) from a failed one, without re-deriving.
    """

    submitted_count: int = Field(description="Number of URLs accepted in the request.")
    pages: list[ExtractedPage] = Field(description="One record per submitted URL, in submitted order.")
