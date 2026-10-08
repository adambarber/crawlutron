from __future__ import annotations

from enum import StrEnum

from app.vocabulary.extraction import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
)


class CrawlExtractionMethod(StrEnum):
    """Output/parsing pipeline applied to content a fetch strategy retrieves.

    Orthogonal to the fetch strategy: any fetch strategy may be paired with any
    extraction method. ``FULL`` is the default unified pipeline (trafilatura HTML
    main text, markitdown for documents, plain-text passthrough). ``TRAFILATURA``
    forces HTML main-text extraction only. ``DOCUMENT`` forces markitdown
    document extraction for PDFs and other non-HTML bodies.
    """

    FULL = EXTRACTION_FULL
    TRAFILATURA = EXTRACTION_TRAFILATURA
    DOCUMENT = EXTRACTION_DOCUMENT
    DOM = EXTRACTION_DOM
