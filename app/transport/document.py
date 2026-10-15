"""Document extraction strategy.

PDF/Office documents are extracted into Markdown. For PDFs we use
``pdfplumber`` (replacing the previous subprocess ``pdftotext`` call) so we can
preserve tabular structure: extracted tables are emitted as GitHub-flavored
Markdown tables rather than being flattened into prose.
"""

from __future__ import annotations

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pdfplumber

from app.models.normalized_url import NormalizedUrl
from app.transport.timeouts import Timeouts

if TYPE_CHECKING:
    from app.transport import pinned_transport


def format_table(rows: list[list[Any]]) -> str:
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
            rendered = format_table(table)
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


def extract_pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    """Extract Markdown (text + tables) from raw PDF bytes using pdfplumber."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return _pdf_to_markdown(pdf)


async def _afetch_document_bytes(url: NormalizedUrl, pin: pinned_transport.PinRule | None = None) -> bytes:
    """Fetch a document's bytes over the shared pinned transport.

    This used to be a hand-rolled ``urllib`` loop, and it was the last one: every
    other fetch in ``app/`` already walked redirects through
    :func:`pinned_transport.apinned_hops`. Spelling the walk again here meant the
    document path carried its own copy of rules it had to keep in step with the
    shared ones, and it had already fallen behind -- the copy had no cross-host
    credential strip, because only the shared chain has one.

    Now every hop is re-validated against the SSRF blocklist and re-pinned to its
    resolved IP by the same chain the HTML fetcher uses, the hop budget is that
    chain's, and ``follow_redirects=False`` keeps httpx from connecting to a
    target nobody checked. The read is capped so a hostile infinite body cannot
    buffer into memory before extraction sees it.
    """
    from app.extraction.content import EXTRACTION_DOC_MAX_INPUT_BYTES
    from app.safety.bounded_io import read_capped_async
    from app.transport import http_client_pool, pinned_transport, pooled_clients

    await pooled_clients._drain_pending_client_closes()
    client = http_client_pool.get_async_http_client()
    timeout = Timeouts.defaults().as_httpx()
    async for chain, pinned_url, hop_headers in pinned_transport.apinned_hops(url, {}, pin=pin):
        # Follow_redirects=False: the chain below is the redirect loop, so httpx
        # following one too would connect to a target nobody re-validated.
        async with client.stream(
            "GET", pinned_url, headers=hop_headers, timeout=timeout, follow_redirects=False
        ) as response:
            if chain.advance(response):
                continue
            return await read_capped_async(response.aiter_raw(), EXTRACTION_DOC_MAX_INPUT_BYTES)
    raise AssertionError("apinned_hops raises on a body that neither returned nor advanced")


def default_document_text_extractor(url: NormalizedUrl) -> str:
    """Default document text extractor: fetch the URL and extract Markdown.

    Used by the browser strategy for ``.pdf`` final URLs. Injectable via
    ``BrowserRenderer(pdf_text_extractor=...)`` for tests.

    SSRF is inherited rather than reimplemented, twice over: the URL arrives
    already admitted (a caller's ``NormalizedUrl`` construction already
    refused an unsafe target loudly, before this function can turn it into
    empty text), and the fetch goes through
    :func:`pinned_transport.apinned_hops`, the same walk every other fetch
    uses, so every redirect hop is re-validated against the SSRF blocklist
    and re-pinned to a resolved IP before anything connects.

    The loud failure is therefore the caller's constructor call, and it is a
    separate obligation from the per-hop check: a refused target raises at
    admission rather than degrading to empty text, so a caller can tell "this
    target was refused" from "this document had no text". The old pre-flight
    ``SafeUrl.parse`` pin duplicated the walk's first resolve; that duplicate
    retired with ``SafeUrl``.
    """
    try:
        # the one production caller runs this through asyncio.to_thread, so a
        # running loop is not the only way in, and a nested loop raises.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pdf_bytes = pool.submit(asyncio.run, _afetch_document_bytes(url)).result()
    except Exception:  # fetch failure yields empty text
        return ""
    try:
        return extract_pdf_bytes_to_markdown(pdf_bytes)
    except Exception:  # unparseable PDFs fall through
        return ""
