"""Bounded-I/O primitives: the single audited place body reads get capped.

Same-shape-safety-logic lens (chokepoints review): every layer used to cap
input differently -- httpx streaming inline, cffi via the queue consumer,
decompression internally -- and the document extractor held a bare
``response.read()`` (round-4 N2). Every body acquisition in strategies/
must go through one of these helpers; the AST invariant in
tests/invariants/test_url_doors.py bans bare ``.read()``/``.content`` access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class BodyTooLarge(Exception):
    """A stream exceeded its byte budget."""


async def read_capped_async(chunks: AsyncIterator[bytes], cap: int) -> bytes:
    """Consume an async byte stream, capped.

    Async counterpart for httpx-style ``aiter_raw()`` streams; raises
    BodyTooLarge the moment the budget is exceeded so the connection is
    abandoned early instead of buffering a hostile body.
    """
    raw = bytearray()
    async for chunk in chunks:
        raw.extend(chunk)
        if len(raw) > cap:
            raise BodyTooLarge(f"streamed body exceeds {cap}-byte cap")
    return bytes(raw)


#: Resource bound for handing a whole HTML document to an in-process text
#: analysis. The two original consumers (the noise-stripping scanner and the
#: lxml metadata extractor) each declared their own 4 MB cap with the same
#: value and the same reasoning, reachable only under two names that read as
#: unrelated. One number, one place.
MAX_HTML_INPUT_BYTES = 4 * 1024 * 1024


def truncate_html_input(html: str | None) -> str:
    """Bound an HTML document to :data:`MAX_HTML_INPUT_BYTES` characters.

    Truncates rather than raises, because both callers are extractors: a partial
    document still yields usable text and metadata, whereas refusing the page
    loses the result entirely. Callers keep their own justification for *why*
    they cap; this owns *how much*.
    """
    body = html or ""
    if len(body) > MAX_HTML_INPUT_BYTES:
        return body[:MAX_HTML_INPUT_BYTES]
    return body


__all__ = [
    "MAX_HTML_INPUT_BYTES",
    "BodyTooLarge",
    "read_capped_async",
    "truncate_html_input",
]
