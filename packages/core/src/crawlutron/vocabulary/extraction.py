"""How retrieved bytes are parsed, and what the pipeline reports about it.

Extraction methods are a policy choice orthogonal to fetch strategy, plus the
``extraction_method`` codes labeling provenance; parsing lives in ``crawlutron.extraction``.
"""

from __future__ import annotations

from typing import Final

# Extraction methods (how retrieved bytes are parsed into text/markdown):
EXTRACTION_FULL: str = "full"
EXTRACTION_DOCUMENT: str = "document"
EXTRACTION_DOM: str = "dom"

# Human-readable descriptions for discovery/agent-hints.
EXTRACTION_DESCRIPTIONS: dict[str, str] = {
    EXTRACTION_FULL: "Unified in-house pipeline for HTML main text, documents, and plain text.",
    EXTRACTION_DOCUMENT: "Document/body path for PDFs and non-HTML responses via the in-house extractor.",
    EXTRACTION_DOM: "DOM-text passthrough without the document or main-text paths.",
}

# extraction_method result codes (diagnostic/provenance labels in crawl results)
EXTRACTION_METHOD_HTTP_HTML: str = "http_html"
EXTRACTION_METHOD_HTTP_BODY: str = "http_body"
EXTRACTION_METHOD_DOCUMENT: str = "document_extract"
EXTRACTION_METHOD_DOM_TEXT: str = "dom_text"
EXTRACTION_METHOD_BINARY_NO_TEXT: Final = "binary_no_text"
