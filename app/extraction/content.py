"""Unified extraction entry: every strategy funnels bytes through one shape."""

from __future__ import annotations

from app.extraction.discovery import (
    detect_pagination,
    extract_discovered_links,
    extract_js_endpoints,
    extract_post_fetch_discovery,
    normalized_extraction,
)
from app.extraction.documents import (
    _DOC_EXTENSIONS,
    _PDF_MAGIC,
    DOC_POOL,
    EXTRACTION_DOC_MAX_INPUT_BYTES,
    ExtractedContent,
    Selectors,
    extract_document,
    extract_html,
    extract_selected,
    html_to_markdown,
    logger,
    shutdown_doc_extraction_executor,
)
from app.extraction.llm_format import text_from_html
from app.extraction.trees import (
    _FrameCollector,
    _walkable,
    forms_from,
    frames_from,
    html_diagnostics,
    parse_count,
    parse_html_tree,
)
from app.models.normalized_url import NormalizedUrl

__all__ = [
    "DOC_POOL",
    "EXTRACTION_DOC_MAX_INPUT_BYTES",
    "ExtractedContent",
    "Selectors",
    "detect_pagination",
    "extract_content",
    "extract_discovered_links",
    "extract_document",
    "extract_html",
    "extract_js_endpoints",
    "extract_post_fetch_discovery",
    "extract_selected",
    "forms_from",
    "frames_from",
    "html_diagnostics",
    "html_to_markdown",
    "is_html",
    "looks_like_document",
    "normalized_extraction",
    "parse_count",
    "parse_html_tree",
    "_FrameCollector",
    "_walkable",
    "shutdown_doc_extraction_executor",
    "text_from_html",
]


def _decode(body: bytes | bytearray | str) -> str:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
    return body


def is_html(content_type: str | None, body: bytes | str) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    text = _decode(body).lstrip().lower()
    return text.startswith("<html") or text.startswith("<!doctype html>")


def looks_like_document(content_type: str | None, body: bytes | str, url: NormalizedUrl | None) -> bool:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return True
    if "html" in ct:
        return False
    lower_url = (url.raw if url is not None else "").split("?")[0].lower()
    if any(lower_url.endswith(ext) for ext in _DOC_EXTENSIONS):
        return True
    if isinstance(body, bytes) and body.startswith(_PDF_MAGIC):
        return True
    if ct in ("application/json", "text/csv", "application/xml", "text/xml", "application/epub+zip"):
        return True
    return False


def extract_content(
    content: bytes | str,
    content_type: str | None,
    url: NormalizedUrl | None,
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """Extract ``(text, markdown)`` from raw content using the unified pipeline.

    The HTML branch hands the selector pair down rather than dispatching on it
    here: ``extract_html`` already routes a selector to :func:`extract_selected`
    and a bare page to trafilatura, so deciding it in both places was two copies
    of one rule that could disagree about the same page.

    A selector only means something to a DOM. Applied to a document or a plain
    text body there is no DOM to target, so the pair cannot be honoured and the
    full content is extracted instead. That is logged rather than dropped in
    silence: the caller asked to narrow and is being handed the whole thing, and
    the only other signal it gets is a result that looks like a successful
    narrowing. The same request against an HTML page does narrow, so the
    difference between the two is invisible on the wire otherwise.
    """
    # ONE decision, read twice (drop report + dispatch): the predicate decodes
    # the whole body, so two evaluations meant two full decodes per non-HTML request.
    html_body = is_html(content_type, content)
    if selectors and not html_body:
        logger.warning(
            "extraction_selector_ignored_non_html",
            extra={
                "css_selector": selectors.css,
                "xpath": selectors.xpath,
                "content_type": content_type,
                "url": url.raw if url is not None else None,
            },
        )
    if html_body:
        return extract_html(_decode(content), url, selectors)
    if looks_like_document(content_type, content, url):
        body = (
            bytes(content)
            if isinstance(content, (bytes, bytearray, memoryview))
            else content.encode("utf-8", "replace")
        )
        return extract_document(body, url)
    text = _decode(content).strip()
    return ExtractedContent(text or None, text or None)
