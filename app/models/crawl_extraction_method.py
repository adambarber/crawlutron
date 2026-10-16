from __future__ import annotations

from enum import StrEnum

from app.vocabulary.extraction import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
)


class CrawlExtractionMethod(StrEnum):
    """Output/parsing pipeline applied to fetched content, orthogonal to fetch strategy.

    ``FULL`` is the unified default (HTML main text, documents, plain text);
    ``TRAFILATURA`` forces HTML main-text only; ``DOCUMENT`` forces markitdown for non-HTML.
    """

    FULL = EXTRACTION_FULL
    TRAFILATURA = EXTRACTION_TRAFILATURA
    DOCUMENT = EXTRACTION_DOCUMENT
    DOM = EXTRACTION_DOM
