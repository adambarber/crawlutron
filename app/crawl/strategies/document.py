"""Document extraction strategy.

PDF/Office documents are extracted into Markdown. For PDFs we use
``pdfplumber`` (replacing the previous subprocess ``pdftotext`` call) so we can
preserve tabular structure: extracted tables are emitted as GitHub-flavored
Markdown tables rather than being flattened into prose.
"""

from __future__ import annotations

import io
import urllib.request
from typing import Any


def _format_table(rows: list[list[Any]]) -> str:
    """Render a single extracted table (list of rows) as a Markdown table."""
    if not rows:
        return ""
    cleaned = []
    for row in rows:
        cells = ["" if cell is None else str(cell).replace("\n", " ").strip() for cell in row]
        cleaned.append(cells)
    out: list[str] = []
    header = cleaned[0]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for row in cleaned[1:]:
        padded = (row + [""] * len(header))[: len(header)]
        out.append("| " + " | ".join(padded) + " |")
    return "\n".join(out)


def _extract_pdf_tables_as_markdown(pdf: Any) -> str:
    """Extract every table across all pages as concatenated Markdown tables."""
    tables: list[str] = []
    for page in getattr(pdf, "pages", []) or []:
        for table in page.extract_tables() or []:
            rendered = _format_table(table)
            if rendered:
                tables.append(rendered)
    return "\n\n".join(tables)


def _pdf_to_markdown(pdf: Any) -> str:
    """Convert an already-opened pdfplumber PDF object to Markdown.

    Page text is concatenated first, then any detected tables are appended as
    Markdown tables so downstream consumers keep structured data.
    """
    text_parts: list[str] = []
    for page in getattr(pdf, "pages", []) or []:
        page_text = page.extract_text() or ""
        if page_text.strip():
            text_parts.append(page_text)
    tables_md = _extract_pdf_tables_as_markdown(pdf)
    body = "\n\n".join(text_parts).strip()
    if tables_md:
        body = (body + "\n\n" + tables_md).strip() if body else tables_md
    return body


def _extract_pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    """Extract Markdown (text + tables) from raw PDF bytes using pdfplumber."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return _pdf_to_markdown(pdf)


def _default_document_text_extractor(url: str) -> str:
    """Default document text extractor: fetch the URL and extract Markdown.

    Used by the browser strategy for ``.pdf`` final URLs. Injectable via
    ``BrowserRenderer(pdf_text_extractor=...)`` for tests.
    """
    import pdfplumber

    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - public crawled URL
            pdf_bytes = response.read()
    except Exception:  # extraction failure yields empty text
        return ""
    try:
        return _extract_pdf_bytes_to_markdown(pdf_bytes)
    except Exception:  # unparseable PDFs fall through
        return ""
