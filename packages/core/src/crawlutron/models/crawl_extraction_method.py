from __future__ import annotations

from enum import StrEnum

from crawlutron.vocabulary.extraction import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
)


class CrawlExtractionMethod(StrEnum):
    """Output/parsing pipeline applied to fetched content, orthogonal to fetch strategy.

    ``FULL`` is the unified default (HTML main text, documents, plain text);
    ``TRAFILATURA`` selects HTML-only main text; ``DOCUMENT`` selects the document path.
    """

    FULL = EXTRACTION_FULL
    TRAFILATURA = EXTRACTION_TRAFILATURA
    DOCUMENT = EXTRACTION_DOCUMENT
    DOM = EXTRACTION_DOM
