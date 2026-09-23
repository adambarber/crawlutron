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

from app.timeouts import Timeouts
from app.validation import HostPolicy


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


# Maximum redirect hops followed when fetching a document. Each hop is
# re-validated against the SSRF guard (H2); beyond the budget the fetch
# fails closed with empty text.
_DOCUMENT_MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urlopen's automatic redirect-following.

    Redirects are followed manually in the fetch loop below so every hop can
    be re-validated against the SSRF guard first (H2).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_document_text_extractor(url: str, policy: HostPolicy) -> str:
    """Default document text extractor: fetch the URL and extract Markdown.

    Used by the browser strategy for ``.pdf`` final URLs. Injectable via
    ``BrowserRenderer(pdf_text_extractor=...)`` for tests.

    SSRF (F2, H2): the initial URL is validated against the SSRF blocklist
    before any fetch, automatic redirect-following is disabled, and every
    redirect hop is re-validated before it is fetched -- so the fetcher is
    never pointed at an internal/loopback host, even when a public document
    redirects to one.
    """
    # Fail closed on unsafe targets (internal/loopback/reserved ranges) BEFORE
    # any heavy import or network access (F2). The SafeUrl chokepoint folds
    # normalize + check into parse(); every redirect hop re-validates via
    # redirect_hop() -- there is no raw-str hop path left to forget a step.
    from app.safe_url import SafeUrl

    # Parse BEFORE the fetch try: unsafe input raises loudly (the F2
    # contract); only fetch/extract failures below yield empty text.
    current = SafeUrl.parse(url, policy=policy, pin=True)

    opener = urllib.request.build_opener(_NoAutoRedirect)
    from app.bounded_io import read_capped_sync
    from app.crawl.extraction import _EXTRACTION_DOC_MAX_INPUT_BYTES

    try:
        for _ in range(_DOCUMENT_MAX_REDIRECTS + 1):
            try:
                connect_url, connect_headers = current.connect()
                request = urllib.request.Request(connect_url, headers=connect_headers)
                with opener.open(request, timeout=Timeouts.defaults().read) as response:
                    # N2: cap the read itself -- a hostile infinite body must
                    # not buffer into memory before the extraction cap sees it.
                    pdf_bytes = read_capped_sync(response, _EXTRACTION_DOC_MAX_INPUT_BYTES)
                    break
            except urllib.error.HTTPError as hop:
                # Real urllib semantics (N2): with auto-redirect disabled,
                # a redirect surfaces as HTTPError -- opener.open() never
                # returns a 30x response object. Re-validate, then follow.
                location = hop.headers.get("Location")
                if hop.code not in _REDIRECT_STATUSES or not location:
                    raise
                current = current.redirect_hop(location)
                continue
        else:
            return ""
    except Exception:  # extraction failure yields empty text
        return ""
    try:
        return _extract_pdf_bytes_to_markdown(pdf_bytes)
    except Exception:  # unparseable PDFs fall through
        return ""
