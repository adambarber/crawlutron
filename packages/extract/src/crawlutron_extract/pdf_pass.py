"""PDF tier-1 text sweep: zlib-inflate streams, Tj/TJ/'/" operators.

Never returns garbled text as good; below the floor returns empty + low.
"""

from __future__ import annotations

import io
import re
import zlib

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar

from crawlutron_extract.limits import RICH_TEXT_FLOOR, _base_result

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

#: Flag emitted when tier 2 produced the emitted text.
_TIER2_FLAG = "pdf_tier2"

#: Flag emitted when table reconstruction could not form a rectangular grid.
_TABLE_DEGRADED_FLAG = "pdf_table_degraded"

#: Vertical proximity tolerance (in points) for grouping chars into one row.
_ROW_Y_TOLERANCE = 3.0

#: Horizontal gap (in points) that starts a new cell within a row.
#: Must exceed intra-word char spacing (~8-10pt) but stay below column gaps.
_CELL_X_GAP = 20.0

#: A row needs at least this many cells to be treated as a table row.
_MIN_TABLE_CELLS = 2


def extract_pdf(content: bytes, limits) -> dict:
    """Extract text from a PDF: tier-1 sweep, then pdfminer.six tier 2 below floor.

    Never raises; a tier-2 failure keeps the tier-1 result and sets a flag.
    """
    flags: list[str] = []
    tier2_used = False
    pages: list[str] = []
    for m in _STREAM_RE.finditer(content):
        body = m.group(1)
        decompressed = _inflate(body)
        if decompressed is None:
            continue
        pages.append(_text_from_stream(decompressed))

    text, quality = _assemble(pages, limits, flags)

    # Tier 2: pdfminer.six when tier 1 is below the floor (empty or partial).
    if quality == "low" and not text:
        tier_text, tier_quality, tier_flags, tier_tables, tier_degraded = _tier2_extract(content, limits)
        if tier_quality in ("high", "medium") or tier_text:
            text = tier_text
            quality = tier_quality
            flags = tier_flags
            tier2_used = True
            if tier_degraded:
                flags.append(_TABLE_DEGRADED_FLAG)
            if tier_tables:
                text = text.rstrip("\n") + "\n\n" + _tables_to_markdown(tier_tables)

    if tier2_used:
        flags.append(_TIER2_FLAG)

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
    """zlib-decompress a stream body; fall back to raw text; None on total failure."""
    try:
        return zlib.decompress(body).decode("utf-8", errors="replace")
    except Exception:
        return body.decode("utf-8", errors="replace")


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
    quality = "high" if len(text) >= RICH_TEXT_FLOOR else "medium"
    return text, quality


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


def _tier2_extract(content: bytes, limits) -> tuple[str, str, list[str], list, bool]:
    """pdfminer.six tier 2: layout-aware text + table reconstruction. Never raises."""
    try:
        pages = list(extract_pages(io.BytesIO(content), laparams=LAParams()))
    except Exception:
        return "", "low", [], [], False
    page_texts: list[str] = []
    tables: list = []
    degraded = False
    for page in pages:
        text, page_tables, page_degraded = _tier2_page(page)
        page_texts.append(text)
        tables.extend(page_tables)
        degraded = degraded or page_degraded
    text, quality = _assemble(page_texts, limits, [])
    if not text:
        quality = "low"
    return text, quality, [], tables, degraded


def _tier2_page(page) -> tuple[str, list, bool]:
    """Extract one page's plain text and rectangular tables via layout analysis."""
    chars: list[tuple[float, float, str]] = []
    _collect_chars(page, chars)
    text = _chars_to_text(chars)
    grids, degraded = _reconstruct_tables(chars)
    return text, grids, degraded


def _collect_chars(obj, out: list) -> None:
    """Recursively collect (x0, y0, text) for every LTChar on the page."""
    if isinstance(obj, LTChar):
        out.append((round(obj.x0, 1), round(obj.y0, 1), obj.get_text()))
    elif hasattr(obj, "_objs"):
        for child in obj._objs:
            _collect_chars(child, out)


def _chars_to_text(chars: list) -> str:
    """Render collected chars as reading-order text, stripping non-printable."""
    lines: dict[float, list] = {}
    for x, y, ch in chars:
        lines.setdefault(y, []).append((x, ch))
    out: list[str] = []
    for y in sorted(lines, reverse=True):
        row = "".join(ch for _, ch in sorted(lines[y]))
        if any(c.isprintable() or c == "\n" for c in row):
            out.append(row)
    return "\n".join(out)


def _reconstruct_tables(chars: list) -> tuple[list, bool]:
    """Group chars into rows/cells by y/x proximity; return (grids, degraded).

    degraded is True only when a column-aligned table region cannot form a
    rectangular grid; ordinary prose never trips it.
    """
    rows: dict[float, list] = {}
    for x, y, ch in chars:
        placed = False
        for ry in rows:
            if abs(ry - y) <= _ROW_Y_TOLERANCE:
                rows[ry].append((x, ch))
                placed = True
                break
        if not placed:
            rows[y] = [(x, ch)]
    cell_rows: list[tuple[list[float], list[str]]] = []
    for y in sorted(rows, reverse=True):
        row_chars = sorted(rows[y], key=lambda t: t[0])
        start_xs: list[float] = []
        texts: list[str] = []
        cur_x: list[float] = []
        cur_ch: list[str] = []
        prev_x: float | None = None
        for x, ch in row_chars:
            if prev_x is not None and x - prev_x > _CELL_X_GAP:
                start_xs.append(cur_x[0])
                texts.append("".join(cur_ch))
                cur_x = []
                cur_ch = []
            cur_x.append(x)
            cur_ch.append(ch)
            prev_x = x
        if cur_x:
            start_xs.append(cur_x[0])
            texts.append("".join(cur_ch))
        if len(texts) >= _MIN_TABLE_CELLS:
            cell_rows.append((start_xs, texts))
    if len(cell_rows) < 2:
        return [], False
    # Column count = the width shared by at least two rows (a real table).
    counts: dict[int, int] = {}
    for _, texts in cell_rows:
        counts[len(texts)] = counts.get(len(texts), 0) + 1
    col_count = max(counts, key=lambda w: (counts[w], -w)) if counts else 0
    if col_count < 2:
        return [], False
    regular = [r for r in cell_rows if len(r[1]) == col_count]
    if len(regular) < 2:
        return [], False
    # Column-aligned when each column's start-x is consistent across rows.
    aligned = True
    for j in range(col_count):
        xs = [r[0][j] for r in regular]
        if max(xs) - min(xs) > 12.0:
            aligned = False
            break
    if not aligned:
        return [], True
    grid = [texts for _, texts in regular]
    return [grid], False


def _tables_to_markdown(tables: list) -> str:
    """Emit rectangular grids as GFM tables; header row + separator + data rows."""
    blocks: list[str] = []
    for grid in tables:
        if not grid:
            continue
        width = len(grid[0])
        lines = [
            "| " + " | ".join(grid[0]) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
        ]
        for row in grid[1:]:
            lines.append("| " + " | ".join(row) + " |")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
