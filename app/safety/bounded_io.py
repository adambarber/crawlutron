"""Bounded-I/O primitives: the single audited place body reads get capped, since
each layer once capped differently (and the document path held a bare ``read()``).
Every body acquisition runs through these helpers; an AST invariant bans bare reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class BodyTooLarge(Exception):
    """A stream exceeded its byte budget."""


async def read_capped_async(chunks: AsyncIterator[bytes], cap: int) -> bytes:
    """Consume an async byte stream, capped -- the counterpart for httpx-style
    ``aiter_raw()`` streams. Raises ``BodyTooLarge`` as soon as the budget is hit
    so the connection is dropped instead of buffering a hostile body.
    """
    raw = bytearray()
    async for chunk in chunks:
        raw.extend(chunk)
        if len(raw) > cap:
            raise BodyTooLarge(f"streamed body exceeds {cap}-byte cap")
    return bytes(raw)


#: Resource bound for handing a whole HTML document to in-process text analysis: one
#: number, one place, where the two consumers each declared their own 4 MB cap.
MAX_HTML_INPUT_BYTES = 4 * 1024 * 1024


def truncate_html_input(html: str | None) -> str:
    """Bound an HTML document to :data:`MAX_HTML_INPUT_BYTES`, truncating rather
    than raising: a partial document still yields usable text, so a refused page
    would lose the result entirely. Callers keep the "why"; this owns "how much".
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
