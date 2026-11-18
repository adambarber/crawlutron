"""The extract() entry point: bounded, deterministic, never-raise.

Slice 3: HTML single-pass extraction with quality ladder.
Slice 4: CSV/JSON/XML text format extraction.
"""

from __future__ import annotations

from typing import cast

from safe_url import HostPolicy, NormalizedUrl

from crawlutron_extract.charset import decode_content
from crawlutron_extract.html_pass import extract_html
from crawlutron_extract.limits import RICH_TEXT_FLOOR, ExtractLimits
from crawlutron_extract.models import Extraction, Quality
from crawlutron_extract.text_format import extract_text_format

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


def _resolve_text_format_quality(quality_str: str) -> Quality:
    """Map text format quality string to Quality enum."""
    mapping = {
        "high": Quality.HIGH,
        "medium": Quality.MEDIUM,
        "low": Quality.LOW,
        "failed": Quality.FAILED,
    }
    return mapping.get(quality_str, Quality.FAILED)


def _apply_text_format(
    fmt: dict[str, object],
    metadata: dict[str, str],
    flags: list[str],
) -> tuple[
    str,
    str,
    list[NormalizedUrl],
    list,
    list,
    list[list[list[str]]],
    bool,
    Quality,
]:
    """Apply a text-format result dict to local variables.

    Shared by CSV/JSON/XML branches to avoid duplicating the same
    dict-unpacking logic three times.
    """
    metadata.update(cast(dict[str, str], fmt["metadata"]))
    text = cast(str, fmt["text"])
    markdown = cast(str, fmt["markdown"])
    links = cast(list[NormalizedUrl], fmt["links"])
    frames = cast(list[dict[str, str]], fmt["frames"])
    forms = cast(list[dict[str, str]], fmt["forms"])
    tables = cast(list[list[list[str]]], fmt["tables"])
    truncated = cast(bool, fmt["truncated"])
    flags.extend(cast(list[str], fmt.get("quality_flags", [])))
    quality = _resolve_text_format_quality(cast(str, fmt.get("_quality", "failed")))
    return text, markdown, links, frames, forms, tables, truncated, quality


def _empty_extraction(
    *,
    quality: Quality,
    flag: str,
    flags: list[str],
    metadata: dict[str, str],
    charset: str | None,
) -> Extraction:
    """Build an empty Extraction with the given quality and flag."""
    return Extraction(
        quality=quality,
        flags=flags + [flag],
        metadata=metadata,
        text="",
        markdown="",
        links=[],
        frames=[],
        forms=[],
        tables=[],
        truncated=False,
        charset=charset,
    )


def extract(
    content: bytes,
    content_type: str,
    *,
    url: str | None = None,
    limits: ExtractLimits = DEFAULT_LIMITS,
) -> Extraction:
    """Extract bounded, deterministic output. Refuses oversized input.

    Never raises on hostile input; returns quality="failed" instead."""
    if len(content) > limits.max_input_bytes:
        return Extraction(quality=Quality.FAILED, flags=["input_refused"])
    metadata: dict[str, str] = {}
    if content_type:
        metadata["content_type"] = content_type
    if url is not None:
        metadata["url"] = url

    ctype = content_type.lower()
    container_types = (
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/epub+zip",
    )

    # Binary containers skip the charset ladder — decode_content would emit
    # charset_lossy on non-text bytes. Return empty with quality=low.
    if ctype in container_types:
        charset: str | None = None
        flags: list[str] = []
        return _empty_extraction(
            quality=Quality.LOW,
            flag=f"{ctype.split('/')[-1]}_unsupported",
            flags=flags,
            metadata=metadata,
            charset=charset,
        )
    # Truly unsupported type: refuse without decode.
    if not (
        ctype == "text/html"
        or ctype.startswith("text/html;")
        or ctype == "text/csv"
        or ctype.startswith("text/csv;")
        or ctype == "application/csv"
        or ctype == "application/json"
        or ctype.startswith("application/json;")
        or ctype == "application/jsonl"
        or ctype.startswith("application/jsonl;")
        or ctype == "text/json"
        or ctype == "application/xml"
        or ctype.startswith("application/xml;")
        or ctype == "text/xml"
        or ctype.startswith("text/xml;")
        or ctype.startswith("text/")
    ):
        return _empty_extraction(
            quality=Quality.FAILED,
            flag="format_refused",
            flags=[],
            metadata=metadata,
            charset=None,
        )

    text, charset, flags = decode_content(content, content_type)
    if charset:
        metadata["charset"] = charset

    markdown = ""
    links: list = []
    frames: list = []
    forms: list = []
    tables: list[list[list[str]]] = []
    truncated = False
    quality = Quality.FAILED
    links_refused = 0

    is_html = ctype == "text/html" or ctype.startswith("text/html;")
    is_csv = ctype == "text/csv" or ctype.startswith("text/csv;") or ctype == "application/csv"
    is_json = (
        ctype == "application/json"
        or ctype.startswith("application/json;")
        or ctype == "application/jsonl"
        or ctype.startswith("application/jsonl;")
        or ctype == "text/json"
    )
    is_xml = (
        ctype == "application/xml"
        or ctype.startswith("application/xml;")
        or ctype == "text/xml"
        or ctype.startswith("text/xml;")
    )
    if is_html:
        policy = HostPolicy.unrestricted()
        html = extract_html(text, limits, base_url=url, policy=policy)
        metadata.update(html["metadata"])
        text = html["text"]
        markdown = html["markdown"]
        links = html["links"]
        frames = cast(list[dict[str, str]], html["frames"])
        forms = cast(list[dict[str, str]], html["forms"])
        tables = html["tables"]
        truncated = html["truncated"]
        if html["links_refused"] > 0:
            flags.append("links_refused")
        links_refused = html["links_refused"]
        quality = _resolve_quality(
            content_type=content_type,
            text=text,
            flags=flags,
            found_main=html["found_main"],
            html_links=links,
            html_frames=frames,
            html_forms=forms,
            html_tables=tables,
        )
    elif is_csv or is_json or is_xml:
        fmt = extract_text_format(text, content_type, limits)
        (
            text,
            markdown,
            links,
            frames,
            forms,
            tables,
            truncated,
            quality,
        ) = _apply_text_format(fmt, metadata, flags)
    elif ctype.startswith("text/"):
        # Plain text: no format extractor ran. Empty → FAILED, else LOW.
        quality = Quality.LOW if text.strip() else Quality.FAILED

    return Extraction(
        quality=quality,
        flags=flags,
        metadata=metadata,
        text=text,
        markdown=markdown,
        links=links,
        frames=frames,
        forms=forms,
        tables=tables,
        truncated=truncated,
        charset=charset,
        links_refused=links_refused,
    )
