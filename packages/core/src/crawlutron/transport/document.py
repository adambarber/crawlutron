"""Document extraction strategy: PDF and Office documents extracted to Markdown.
``pdfplumber`` (replacing a subprocess ``pdftotext``) preserves tabular structure
as GitHub-flavored Markdown tables rather than flattening them to prose.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pdfplumber
from safe_url.normalized_url import NormalizedUrl

from crawlutron.transport.httpx_transport import HttpxTransport
from crawlutron.transport.timeouts import Timeouts

if TYPE_CHECKING:
    from pdfplumber.pdf import PDF

    from crawlutron.transport import pinned_transport


def format_table(rows: list[list[str | None]]) -> str:
    """Render a single extracted table (list of rows) as a Markdown table."""
    if not rows:
        return ""
    cleaned: list[list[str]] = [
        ["" if cell is None else str(cell).replace("\n", " ").strip() for cell in row] for row in rows
    ]
    out: list[str] = []
    header: list[str] = cleaned[0]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for row in cleaned[1:]:
        padded: list[str] = (row + [""] * len(header))[: len(header)]
        out.append("| " + " | ".join(padded) + " |")
    return "\n".join(out)


def _extract_pdf_tables_as_markdown(pdf: PDF) -> str:
    """Extract every table across all pages as concatenated Markdown tables."""
    tables: list[str] = []
    for page in getattr(pdf, "pages", []) or []:
        for table in page.extract_tables() or []:
            rendered = format_table(table)
            if rendered:
                tables.append(rendered)
    return "\n\n".join(tables)


def _pdf_to_markdown(pdf: PDF) -> str:
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


def extract_pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    """Extract Markdown (text + tables) from raw PDF bytes using pdfplumber."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return _pdf_to_markdown(pdf)


async def _afetch_document_bytes(
    url: NormalizedUrl,
    pin: pinned_transport.PinRule | None = None,
    *,
    timeout_seconds: float | None = None,
) -> bytes:
    """Fetch document bytes over the single redirect walk: every hop re-pinned,
    body capped per hop. The budget is the caller's request budget; none falls
    to the posture. Extraction plants no hop cookies."""
    from crawlutron.extraction.content import EXTRACTION_DOC_MAX_INPUT_BYTES
    from crawlutron.transport import pooled_clients
    from crawlutron.transport.redirect_walker import RedirectWalker

    await pooled_clients.drain_pending_client_closes()
    # The request budget when one exists, the service posture otherwise --
    # both read FROM the vocabulary, never a local scalar.
    budget = Timeouts.for_request(timeout_seconds) if timeout_seconds is not None else Timeouts.defaults()

    async def _noop_store_cookies(hop_url: NormalizedUrl, response_headers: Mapping[str, str]) -> None:
        """Document extraction is read-only: no hop cookies are planted."""
        return None

    result = await RedirectWalker(HttpxTransport(), store_cookies=_noop_store_cookies).walk(
        url, {}, budget.read, max_bytes=EXTRACTION_DOC_MAX_INPUT_BYTES, pin=pin
    )
    if result is None:
        # The walker always returns a terminal result or raises.
        raise AssertionError("the redirect walker returned no terminal result")
    return result.body


def default_document_text_extractor(url: NormalizedUrl, timeout_seconds: float | None = None) -> str:
    """Default document text extractor: fetch the URL, extract Markdown. Used by
    the browser strategy for ``.pdf`` URLs, injectable for tests. SSRF inherited:
    the URL arrives admitted, the fetch walks the shared chain; budget from the posture."""
    try:
        # the one production caller runs this through asyncio.to_thread, so a
        # running loop is not the only way in, and a nested loop raises.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pdf_bytes = pool.submit(
                asyncio.run, _afetch_document_bytes(url, timeout_seconds=timeout_seconds)
            ).result()
    except Exception:  # fetch failure yields empty text
        return ""
    try:
        return extract_pdf_bytes_to_markdown(pdf_bytes)
    except Exception:  # unparseable PDFs fall through
        return ""
