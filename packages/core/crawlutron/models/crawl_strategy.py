from __future__ import annotations

from enum import StrEnum

from crawlutron.vocabulary.strategies import (
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)


class CrawlStrategy(StrEnum):
    """Fetch execution strategies, tried in fallback order.

    These say HOW to retrieve bytes for a URL; parsing them into text/markdown is
    a separate, orthogonal concern handled by ``CrawlExtractionMethod``.
    """

    HTTP_DECLARED_CRAWLER = HTTP_DECLARED_CRAWLER
    HTTP_CFFI_IMPERSONATE = HTTP_CFFI_IMPERSONATE
    BROWSER_HEADLESS = BROWSER_HEADLESS
    BROWSER_HEADED_WARMED = BROWSER_HEADED_WARMED
