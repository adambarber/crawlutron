"""Charset detection and lossy-safe decoding.

BOM -> Content-Type -> <meta charset> -> strict UTF-8 -> lossy. Never raises.
"""

from __future__ import annotations

import re

__all__ = ["detect_charset", "decode_content"]

# BOM -> charset, longest BOM first so UTF-8 (3 bytes) wins over UTF-16 (2 bytes).
_BOM_CHARS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

# Bounded meta-charset scan: first 8 KiB only, no full HTML parse.
_META_CHARSET_RE = re.compile(
    r'<meta[^>]+charset\s*=\s*["\']?([^\s"\']+)',
    re.IGNORECASE,
)
_META_CONTENT_TYPE_RE = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']?content-type["\']?'
    r'[^>]+content\s*=\s*["\']?[^;\s"]*;\s*charset\s*=\s*([^\s"\']+)',
    re.IGNORECASE,
)

_META_SCAN_BYTES = 8192


def _strip_bom(content: bytes) -> tuple[bytes, str | None]:
    """Strip a leading BOM, returning (body, charset) or (content, None)."""
    for bom, charset in _BOM_CHARS:
        if content.startswith(bom):
            return content[len(bom) :], charset
    return content, None


def _parse_content_type_charset(content_type: str) -> str | None:
    """Extract charset= from Content-Type, with or without quotes."""
    if not content_type:
        return None
    match = re.search(r";\s*charset\s*=\s*[\"']?([^\s\";]+)", content_type, re.IGNORECASE)
    return match.group(1).strip().lower() if match else None


def _scan_meta_charset(content: bytes) -> str | None:
    """Scan the first 8 KiB for an HTML meta charset declaration."""
    head = content[:_META_SCAN_BYTES]
    text = head.decode("utf-8", errors="replace")
    for pattern in (_META_CHARSET_RE, _META_CONTENT_TYPE_RE):
        match = pattern.search(text)
        if match:
            return match.group(1).strip().lower()
    return None


def detect_charset(content: bytes, content_type: str) -> str:
    """Resolve charset by priority: BOM -> Content-Type -> meta -> UTF-8."""
    _, bom_charset = _strip_bom(content)
    if bom_charset:
        return bom_charset

    declared = _parse_content_type_charset(content_type)
    if declared:
        return declared

    if "text/html" in content_type.lower():
        meta = _scan_meta_charset(content)
        if meta:
            return meta

    return "utf-8"


def decode_content(content: bytes, content_type: str) -> tuple[str, str, list[str]]:
    """Decode content, never raising: returns (text, charset, flags)."""
    flags: list[str] = []
    body, _bom_charset = _strip_bom(content)

    charset = detect_charset(content, content_type)

    try:
        text = body.decode(charset)
    except (UnicodeDecodeError, LookupError):
        flags.append("charset_lossy")
        try:
            text = body.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, LookupError):
            text = body.decode("latin-1", errors="replace")

    return text, charset, flags
