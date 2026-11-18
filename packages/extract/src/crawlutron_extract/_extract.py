"""The extract() entry point: bounded, deterministic, never-raise.

Slice 3: HTML single-pass extraction (text, markdown, metadata, links,
frames, forms, tables).
"""

from __future__ import annotations

from crawlutron_extract.charset import decode_content
from crawlutron_extract.html_pass import extract_html
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
    quality = Quality.LOW
    html_markdown = ""
    html_links: list[dict[str, str]] = []
    html_frames: list = []
    html_forms: list = []
    html_tables: list[list[list[str]]] = []
    html_truncated = False
    if "text/html" in content_type.lower():
        html = extract_html(text, limits)
        metadata.update(html["metadata"])
        text = html["text"]
        html_markdown = html["markdown"]
        html_links = html["links"]
        html_frames = html["frames"]
        html_forms = html["forms"]
        html_tables = html["tables"]
        html_truncated = html["truncated"]
        quality = Quality.MEDIUM if text else Quality.LOW
    return Extraction(
        quality=quality,
        flags=flags,
        metadata=metadata,
        text=text,
        markdown=html_markdown,
        links=html_links,
        frames=html_frames,
        forms=html_forms,
        tables=html_tables,
        truncated=html_truncated,
        charset=charset,
    )
