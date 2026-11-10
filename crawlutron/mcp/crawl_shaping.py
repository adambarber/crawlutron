"""The MCP crawl doors' response projection: full envelope vs extracted pages."""

from __future__ import annotations

from enum import StrEnum, auto

from crawlutron.models.crawl_and_extract_response import CrawlAndExtractResponse
from crawlutron.models.extracted_page import ExtractedPage

__all__ = ["CrawlProjection", "projected"]


#: One projection of a crawl response, chosen by the door the caller came through.
class CrawlProjection(StrEnum):
    from_crawl = auto()
    from_extract = auto()


async def projected(crawled, projection: CrawlProjection):
    """The crawl response read through the door the caller came through."""
    response = await crawled
    return response if projection is CrawlProjection.from_crawl else await _extracted(response)


async def _extracted(response) -> CrawlAndExtractResponse:
    """The crawl response read as extraction records -- the same projection over the
    same stored data, provenance dropped, so a content-only caller does not pay
    for doubled per-page bulk on the wire.
    """
    return CrawlAndExtractResponse(
        submitted_count=response.submitted_count,
        pages=[
            ExtractedPage(
                url=result.normalized_url,
                status=result.status,
                classification=result.classification,
                title=result.metadata.title,
                markdown=result.markdown,
            )
            for result in response.results
        ],
    )
