"""PDF tier-1 text sweep: zlib-inflate streams, Tj/TJ/'/" operators.

Never returns garbled text as good; below the floor returns empty + low.
"""

from __future__ import annotations

import re
import zlib

from crawlutron_extract.limits import _base_result

__all__ = ["extract_pdf"]

#: Text-showing operators and their preceding operand (array, paren string or hex).
_TEXT_OP_RE = re.compile(rb"(\[(?:[^\[\]]|\\.)*\]|\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f]{0,2000}>)\s*(Tj|TJ|'|\")")

#: stream/endstream pair; capture the (possibly compressed) stream body.
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)

#: Minimum chars on a page for its text to count as good.
_MIN_CHARS_PER_PAGE = 3

#: Minimum printable ratio for a page's text to count as good.
_MIN_PRINTABLE_RATIO = 0.6

#: Quality flag emitted when the result is below the quality floor.
_PARTIAL_FLAG = "pdf_encoding_partial"


def extract_pdf(content: bytes, limits) -> dict:
    """Extract text from a PDF via tier-1 stream sweep. Never raises.

    Returns a dict shaped like the text-format result.
    """
    flags: list[str] = []
    pages: list[str] = []
    for m in _STREAM_RE.finditer(content):
        body = m.group(1)
        decompressed = _inflate(body)
        if decompressed is None:
            continue
        pages.append(_text_from_stream(decompressed))

    text, quality = _assemble(pages, limits, flags)

    result = _base_result()
    result.update(
        {
            "text": text,
            "markdown": text,
            "metadata": {},
            "quality_flags": flags,
            "_quality": quality,
        }
    )
    return result


def _inflate(body: bytes) -> str | None:
    """zlib-decompress a stream body; None on failure."""
    try:
        return zlib.decompress(body).decode("utf-8", errors="replace")
    except Exception:
        return None


def _strip_controls(text: str) -> str:
    """Drop control characters (keep newlines) so garbled text never emits."""
    return "".join(ch for ch in text if ch.isprintable() or ch == "\n")


def _assemble(pages: list[str], limits, flags: list[str]) -> tuple[str, str]:
    """Join good pages; below the floor returns empty + partial flag."""
    good: list[str] = []
    for page in pages:
        chars = len(page)
        if chars == 0:
            continue
        printable = sum(1 for ch in page if ch.isprintable() or ch == "\n")
        if chars >= _MIN_CHARS_PER_PAGE and printable / chars >= _MIN_PRINTABLE_RATIO:
            good.append(page)
    if not good:
        flags.append(_PARTIAL_FLAG)
        return "", "low"
    text = _strip_controls("\n".join(good))
    text, _ = _apply_caps(text, limits)
    return text, "low"


def _apply_caps(text: str, limits) -> tuple[str, bool]:
    truncated = False
    if len(text) > limits.max_text_chars:
        text = text[: limits.max_text_chars]
        truncated = True
    return text, truncated


def _extract_strings(operand: bytes) -> list[str]:
    """Pull the showable strings out of a text-showing operator's operand."""
    if operand.startswith(b"["):
        return _array_strings(operand[1:-1])
    if operand.startswith(b"<"):
        return [_hex_to_text(operand[1:-1])]
    return [_unescape_pdf_string(operand[1:-1])]


def _array_strings(body: bytes) -> list[str]:
    """Strings from a TJ array; numeric elements and adjacent strings add kerning spacing."""
    out: list[str] = []
    prev_was_string = False
    for token in re.findall(rb"\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f]*>|[-+]?[0-9]*\.?[0-9]+", body):
        if token.startswith(b"(") or token.startswith(b"<"):
            if prev_was_string:
                out.append(" ")
            if token.startswith(b"("):
                out.append(_unescape_pdf_string(token[1:-1]))
            else:
                out.append(_hex_to_text(token[1:-1]))
            prev_was_string = True
        else:
            out.append(" ")
            prev_was_string = False
    return out


def _unescape_pdf_string(raw: bytes) -> str:
    """Unescape a PDF literal string to text."""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x5C and i + 1 < n:  # backslash escape
            nxt = raw[i + 1]
            mapping = {0x6E: "\n", 0x52: "\r", 0x74: "\t", 0x62: "\b", 0x66: "\f"}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            if nxt in (0x28, 0x29, 0x5C):  # ( ) \
                out.append(chr(nxt))
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:  # octal \ooo
                digits = []
                j = i + 1
                while j < n and j - (i + 1) < 3 and 0x30 <= raw[j] <= 0x37:
                    digits.append(raw[j])
                    j += 1
                if digits:
                    out.append(chr(int(bytes(digits), 8)))
                    i = j
                    continue
            out.append(chr(nxt))
            i += 2
            continue
        out.append(chr(b))
        i += 1
    return "".join(out)


def _hex_to_text(raw: bytes) -> str:
    """Decode a hex string to text (lossy on odd/truncated input)."""
    try:
        return bytes.fromhex(raw.decode("ascii", errors="replace")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _text_from_stream(decompressed: str) -> str:
    """Gather the strings operated on by Tj/TJ/'/" in one stream, with spacing."""
    parts: list[str] = []
    for m in _TEXT_OP_RE.finditer(decompressed.encode("utf-8", errors="replace")):
        operand, op = m.group(1), m.group(2)
        strings = _extract_strings(operand)
        if op in (b"'", b'"'):
            parts.append("\n")
        parts.extend(strings)
        if op == b"TJ":
            parts.append(" ")
    return "".join(parts)
