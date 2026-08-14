"""Single source of truth for policy/strategy/extraction magic strings.

These literals are consumed by ``app.service``, ``app.api_models``,
``app.discovery`` and the test suite so that spelling drift (or a stray
non-ASCII look-alike character) cannot desynchronize comparisons. Always
import the constant instead of typing the string literal.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Fetch strategies (how bytes are retrieved). Orthogonal to extraction.
# ---------------------------------------------------------------------------
HTTP_DECLARED_CRAWLER: str = "HTTP_DECLARED_CRAWLER"
HTTP_CFFI_IMPERSONATE: str = "HTTP_CFFI_IMPERSONATE"
BROWSER_HEADLESS: str = "BROWSER_HEADLESS"
BROWSER_HEADED_WARMED: str = "BROWSER_HEADED_WARMED"

# Strategy ordering / registries (imported by service.py)
HTTP_STRATEGIES: set[str] = {HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE}
BROWSER_STRATEGIES: set[str] = {BROWSER_HEADLESS, BROWSER_HEADED_WARMED}
SUPPORTED_STRATEGIES: set[str] = HTTP_STRATEGIES | BROWSER_STRATEGIES
DEFAULT_STRATEGIES: list[str] = [
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
]

# ---------------------------------------------------------------------------
# Extraction methods (how retrieved bytes are parsed into text/markdown).
# These are output-pipeline options, strictly orthogonal to fetch strategies.
# ---------------------------------------------------------------------------
EXTRACTION_FULL: str = "full"
EXTRACTION_TRAFILATURA: str = "trafilatura"
EXTRACTION_DOCUMENT: str = "document"
EXTRACTION_DOM: str = "dom"

SUPPORTED_EXTRACTIONS: list[str] = [
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
]
DEFAULT_EXTRACTION: str = EXTRACTION_FULL

# Human-readable descriptions for discovery/agent-hints.
EXTRACTION_DESCRIPTIONS: dict[str, str] = {
    EXTRACTION_FULL: "Unified output pipeline: trafilatura HTML main text, markitdown for documents, plain-text passthrough.",
    EXTRACTION_TRAFILATURA: "HTML-only main-text extraction for noisy HTML; does not parse PDF or binary documents.",
    EXTRACTION_DOCUMENT: "Document/body extraction via markitdown for PDFs and non-HTML responses when text evidence is needed.",
    EXTRACTION_DOM: "DOM-text extraction: rendered or parsed page text passthrough without the document or Trafilatura pipelines.",
}

# ---------------------------------------------------------------------------
# extraction_method result codes (diagnostic/provenance labels in crawl results).
# ---------------------------------------------------------------------------
EXTRACTION_METHOD_HTTP_HTML: str = "http_html"
EXTRACTION_METHOD_HTTP_BODY: str = "http_body"
EXTRACTION_METHOD_DOCUMENT: str = "document_extract"
EXTRACTION_METHOD_TRAFILATURA: str = "trafilatura_extract"
EXTRACTION_METHOD_DOM_TEXT: str = "dom_text"
EXTRACTION_METHOD_BINARY_NO_TEXT: str = "binary_no_text"
