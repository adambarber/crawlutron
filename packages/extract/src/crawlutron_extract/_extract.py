"""The extract() entry point: bounded, deterministic, never-raise.

Slice 1 skeleton: input refusal + empty payload. Slices 2-8 add parsers.
"""

from __future__ import annotations

from crawlutron_extract.charset import decode_content
from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.models import Extraction, Quality

__all__ = ["DEFAULT_LIMITS", "extract"]

DEFAULT_LIMITS = ExtractLimits()


def extract(
    content: bytes,
    content_type: str,
    *,
    url: str | None = None,
    limits: ExtractLimits = DEFAULT_LIMITS,
) -> Extraction:
    """Extract bounded, deterministic output. Refuses oversized input.

    Never raises on hostile input; returns quality="failed" instead. Only
    programmer error (a bad type) may raise.
    """
    if len(content) > limits.max_input_bytes:
        return Extraction(quality=Quality.FAILED, flags=["input_refused"])
    text, charset, flags = decode_content(content, content_type)
    metadata: dict[str, str] = {}
    if content_type:
        metadata["content_type"] = content_type
    if url is not None:
        metadata["url"] = url
    if charset:
        metadata["charset"] = charset
    # No extractor has run yet: quality reports confidence in the extracted
    # payload, not the decode step. Decode facts live in flags (charset_lossy).
    return Extraction(
        quality=Quality.LOW,
        flags=flags,
        metadata=metadata,
        text=text,
        charset=charset,
    )
