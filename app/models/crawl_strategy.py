from __future__ import annotations

from enum import StrEnum

from app.vocabulary.strategies import (
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)


class CrawlStrategy(StrEnum):
    """Fetch execution strategies, tried in fallback order.

    These describe *how* to retrieve bytes for a URL. How those bytes are parsed
    into text/markdown is a separate concern handled by
    :class:`CrawlExtractionMethod` (the ``extraction`` policy field), orthogonal
    to the fetch strategy.
    """

    HTTP_DECLARED_CRAWLER = HTTP_DECLARED_CRAWLER
    HTTP_CFFI_IMPERSONATE = HTTP_CFFI_IMPERSONATE
    BROWSER_HEADLESS = BROWSER_HEADLESS
    BROWSER_HEADED_WARMED = BROWSER_HEADED_WARMED
