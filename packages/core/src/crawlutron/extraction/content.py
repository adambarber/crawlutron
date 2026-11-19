"""Unified extraction entry: every strategy funnels bytes through one shape."""

from __future__ import annotations

from crawlutron_extract import extract as _package_extract
from crawlutron_extract.charset import decode_content as _package_decode_content
from safe_url.normalized_url import NormalizedUrl

from crawlutron.extraction.discovery import (
    PostFetchDiscovery,
    detect_pagination,
    extract_discovered_links,
    extract_js_endpoints,
    extract_post_fetch_discovery,
    normalized_extraction,
)
from crawlutron.extraction.documents import (
    DOC_EXTENSIONS,
    DOC_POOL,
    EXTRACTION_DOC_MAX_INPUT_BYTES,
    PDF_MAGIC,
    ExtractedContent,
    Selectors,
    extract_document,
    extract_html,
    extract_selected,
    html_to_markdown,
    logger,
    shutdown_doc_extraction_executor,
)
from crawlutron.extraction.llm_format import text_from_html
from crawlutron.extraction.trees import (
    FrameCollector,
    forms_from,
    frames_from,
    html_diagnostics,
    parse_count,
    parse_html_tree,
    walkable,
)

__all__ = [
    "DOC_POOL",
    "EXTRACTION_DOC_MAX_INPUT_BYTES",
    "ExtractedContent",
    "Selectors",
    "decode_body",
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
    "FrameCollector",
    "walkable",
    "shutdown_doc_extraction_executor",
    "text_from_html",
    "PostFetchDiscovery",
]


def decode_body(body: bytes | bytearray | str, content_type: str | None = None) -> str:
    """Decode a body through the package charset ladder: BOM -> Content-Type -> meta -> UTF-8.

    A ``str`` body is already text and is returned unchanged; no decode runs.
    """
    if isinstance(body, str):
        return body
    text, _charset, _flags = _package_decode_content(bytes(body), content_type or "")
    return text


def _is_html_text(text: str) -> bool:
    """The sniff over already-decoded text, shared by ``is_html`` and ``extract_content``."""
    return text.lstrip().lower().startswith(("<html", "<!doctype html>"))


def is_html(content_type: str | None, body: bytes | str, *, decoded: str | None = None) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    return _is_html_text(decoded if decoded is not None else decode_body(body))


def looks_like_document(content_type: str | None, body: bytes | str, url: NormalizedUrl | None) -> bool:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return True
    if "html" in ct:
        return False
    lower_url = (url.raw if url is not None else "").split("?")[0].lower()
    if any(lower_url.endswith(ext) for ext in DOC_EXTENSIONS):
        return True
    if isinstance(body, bytes) and body.startswith(PDF_MAGIC):
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
    """Extract ``(text, markdown)``; HTML→extract_html, documents→extract_document,
    else a thin adapter over ``crawlutron_extract.extract``.
    """
    # ONE decision, read twice (drop report + dispatch): the predicate decodes the whole
    # body, so one decode per request shares its text with the HTML branch.
    decoded = content if isinstance(content, str) else decode_body(content)
    html_body = is_html(content_type, content, decoded=decoded)
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
        return extract_html(decoded, url, selectors)
    if looks_like_document(content_type, content, url):
        body = (
            bytes(content)
            if isinstance(content, (bytes, bytearray, memoryview))
            else content.encode("utf-8", "replace")
        )
        return extract_document(body, url)
    raw = content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8", "replace")
    extraction = _package_extract(raw, content_type or "", url=url.raw if url is not None else None)
    text = extraction.text or None
    markdown = extraction.markdown or text
    if text is None:
        text = decoded.strip() or None
        markdown = text
    return ExtractedContent(text, markdown)
