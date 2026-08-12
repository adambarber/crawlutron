"""Unified content extraction pipeline.

Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels its
raw fetched/rendered bytes through :func:`extract_content`, which returns a
consistent ``(text, markdown)`` pair regardless of content type:

* HTML web pages  -> trafilatura (boilerplate removed, main content kept)
* Documents       -> markitdown (PDF/DOCX/PPTX/XLSX/CSV/JSON/XML/EPub -> Markdown)
* Plain text      -> passed through as Markdown

This keeps the agent-facing output uniform (always Markdown) and token-minded:
web pages lose nav/footer/ads boilerplate, while documents keep their structure.
"""

import io
from typing import NamedTuple

from app.crawl.firecrawl_format import _text_from_html

_PDF_MAGIC = b"%PDF"
_DOC_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".json", ".xml", ".epub",
)


class ExtractedContent(NamedTuple):
    text: str | None
    markdown: str | None


def _decode(body: bytes | str) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body


def _is_html(content_type: str | None, body: bytes | str) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    text = _decode(body).lstrip().lower()
    return text.startswith("<html") or text.startswith("<!doctype html")


def _looks_like_document(content_type: str | None, body: bytes | str, url: str | None) -> bool:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return True
    if "html" in ct:
        return False
    lower_url = (url or "").split("?")[0].lower()
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
    url: str | None,
) -> ExtractedContent:
    """Extract ``(text, markdown)`` from raw content using the unified pipeline."""
    if _is_html(content_type, content):
        return _extract_html(_decode(content), url)
    if _looks_like_document(content_type, content, url):
        body = content if isinstance(content, bytes) else content.encode("utf-8", "replace")
        return _extract_document(body, url)
    text = _decode(content).strip()
    return ExtractedContent(text or None, text or None)


def _extract_html(html: str, url: str | None) -> ExtractedContent:
    try:
        import trafilatura
        md = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            favor_recall=True,
            include_comments=False,
            include_tables=True,
            include_formatting=True,
            include_links=True,
            deduplicate=True,
        )
    except Exception:  # noqa: BLE001  # pragma: no cover - trafilatura unavailable
        md = None
    if not md or not md.strip():
        md = _text_from_html(html) or None  # pragma: no cover - defensive fallback
    return ExtractedContent(md, md)


def _extract_document(body: bytes, url: str | None) -> ExtractedContent:
    try:
        from markitdown import MarkItDown
    except ImportError:  # pragma: no cover - markitdown is a required dependency
        return ExtractedContent(None, None)
    try:
        res = MarkItDown().convert_stream(io.BytesIO(body))
        md = res.text_content if res is not None else None
    except Exception:  # noqa: BLE001  # pragma: no cover - unparseable documents fall through
        md = None
    if not md or not md.strip():
        return ExtractedContent(None, None)
    return ExtractedContent(md, md)
