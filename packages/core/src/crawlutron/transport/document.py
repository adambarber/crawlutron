"""Document extraction: fetch a document URL, extract Markdown via crawlutron_extract.

The package extractor runs off the loop through asyncio.to_thread under an
explicit concurrency cap; failures degrade to empty text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.pdf_pass import extract_pdf as _package_extract_pdf
from safe_url.normalized_url import NormalizedUrl

from crawlutron.transport.httpx_transport import HttpxTransport
from crawlutron.transport.redirect_walker import RedirectWalker
from crawlutron.transport.timeouts import Timeouts

if TYPE_CHECKING:
    from crawlutron.transport import pinned_transport

#: Explicit concurrency cap for document extraction off the event loop.
_DOC_EXTRACTION_CONCURRENCY = 4
_DOC_EXTRACTION_SEMAPHORE = asyncio.Semaphore(_DOC_EXTRACTION_CONCURRENCY)

#: Ceiling on document input bytes fed to the package extractor.
_DOC_EXTRACTION_MAX_BYTES = 20 * 1024 * 1024


async def _afetch_document_bytes(
    url: NormalizedUrl,
    pin: pinned_transport.PinRule | None = None,
    *,
    timeout_seconds: float | None = None,
) -> bytes:
    """Fetch document bytes over the single redirect walk: every hop re-pinned,
    body capped per hop. The budget is the caller's request budget; none falls
    to the posture. Extraction plants no hop cookies."""
    from crawlutron.transport import pooled_clients

    await pooled_clients.drain_pending_client_closes()
    budget = Timeouts.for_request(timeout_seconds) if timeout_seconds is not None else Timeouts.defaults()

    async def _noop_store_cookies(hop_url: NormalizedUrl, response_headers: Mapping[str, str]) -> None:
        """Document extraction is read-only: no hop cookies are planted."""
        return None

    result = await RedirectWalker(HttpxTransport(), store_cookies=_noop_store_cookies).walk(
        url, {}, budget.read, max_bytes=_DOC_EXTRACTION_MAX_BYTES, pin=pin
    )
    if result is None:
        # The walker always returns a terminal result or raises.
        raise AssertionError("the redirect walker returned no terminal result")
    return result.body


async def _capped_extract_pdf(pdf_bytes: bytes) -> str:
    """Extract Markdown text from PDF bytes via the package, under the cap."""
    async with _DOC_EXTRACTION_SEMAPHORE:
        result = await asyncio.to_thread(_package_extract_pdf, pdf_bytes, ExtractLimits())
    return result.get("text") or ""


def default_document_text_extractor(url: NormalizedUrl, timeout_seconds: float | None = None) -> str:
    """Fetch a document URL and extract Markdown via the package.

    Runs off the loop via asyncio.to_thread under a concurrency cap; degrades to
    empty text on failure. Sync signature for the browser strategy.
    """

    async def _full(url: NormalizedUrl) -> str:
        pdf_bytes = await _afetch_document_bytes(url, timeout_seconds=timeout_seconds)
        if not pdf_bytes:
            return ""
        return await _capped_extract_pdf(pdf_bytes)

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-extract") as pool:
            text = pool.submit(asyncio.run, _full(url)).result()
    except Exception:  # fetch failure yields empty text
        return ""
    return text or ""
