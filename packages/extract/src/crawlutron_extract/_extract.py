"""The extract() entry point: bounded, deterministic, never-raise.

Slice 3: HTML single-pass extraction with quality ladder.
"""

from __future__ import annotations

from crawlutron_extract.charset import decode_content
from crawlutron_extract.html_pass import extract_html
from crawlutron_extract.limits import RICH_TEXT_FLOOR, ExtractLimits
from crawlutron_extract.models import Extraction, Quality

__all__ = ["DEFAULT_LIMITS", "extract"]

DEFAULT_LIMITS = ExtractLimits()


def _resolve_quality(
    *,
    content_type: str,
    text: str,
    flags: list[str],
    found_main: bool,
    html_links: list,
    html_frames: list,
    html_forms: list,
    html_tables: list,
) -> Quality:
    """Apply the quality ladder: FAILED < LOW < MEDIUM < HIGH."""
    lossy = "charset_lossy" in flags
    has_body_text = len(text.strip()) > 0
    has_content = (
        has_body_text
        or len(html_links) > 0
        or len(html_frames) > 0
        or len(html_forms) > 0
        or len(html_tables) > 0
    )

    if not has_content:
        return Quality.FAILED

    if "text/html" not in content_type.lower():
        # Non-HTML: no main-content concept; decoded text alone is LOW.
        return Quality.LOW

    if lossy:
        return Quality.LOW

    if not has_body_text:
        # Only boilerplate/links/frames/forms were found.
        return Quality.LOW

    if found_main and len(text) >= RICH_TEXT_FLOOR:
        return Quality.HIGH

    return Quality.MEDIUM


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
    quality = Quality.FAILED
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
        quality = _resolve_quality(
            content_type=content_type,
            text=text,
            flags=flags,
            found_main=html["found_main"],
            html_links=html_links,
            html_frames=html_frames,
            html_forms=html_forms,
            html_tables=html_tables,
        )
    else:
        quality = _resolve_quality(
            content_type=content_type,
            text=text,
            flags=flags,
            found_main=False,
            html_links=[],
            html_frames=[],
            html_forms=[],
            html_tables=[],
        )
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
