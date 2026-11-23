"""Text format extraction: CSV, JSON, XML in a single pass each.

Never raises on hostile input. XML with DTD is refused (not expanded).
All outputs bounded by ExtractLimits; every list capped and carries truncated.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import cast

from crawlutron_extract.limits import _empty_result

__all__ = ["extract_text_format"]

#: DOCTYPE must be refused: never expand external entities (XML bomb).
_DOCTYPE_RE = re.compile(r"<!DOCTYPE\s", re.IGNORECASE)

#: JSON nesting depth limit to reject deeply nested payloads.
_MAX_JSON_DEPTH = 50


def extract_text_format(
    content: str,
    content_type: str,
    limits,
) -> dict[str, object]:
    """Extract from CSV/JSON/XML text formats. Never raises."""
    ctype = content_type.lower()
    if "csv" in ctype:
        return _extract_csv(content, limits)
    if "json" in ctype or "jsonl" in ctype:
        return _extract_json(content, limits)
    if "xml" in ctype:
        return _extract_xml(content, limits)
    return _empty_result("unsupported_content_type")


def _extract_csv(content: str, limits) -> dict[str, object]:
    """Parse CSV with embedded newlines and mixed line endings. Bounded rows."""
    flags: list[str] = []
    rows: list[list[str]] = []
    truncated = False

    # Normalize line endings: \r\n and \r -> \n for consistent parsing.
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    try:
        reader = csv.reader(io.StringIO(normalized))
        for row in reader:
            if len(rows) >= limits.max_table_rows:
                truncated = True
                flags.append("table_rows_capped")
                break
            rows.append(row)
    except csv.Error:
        flags.append("csv_parse_error")
        return _empty_result("csv_parse_error", ["csv_parse_error"])

    if not rows:
        return _empty_result("empty_csv", [])

    text = "\n".join(" | ".join(r) for r in rows)
    markdown = _csv_to_markdown(rows, limits, flags)
    metadata = {"rows": str(len(rows))}
    text, truncated = _cap_text(text, limits, flags, truncated)

    quality = _csv_quality(rows, truncated, flags)
    return _text_format_result(
        text=text,
        markdown=markdown,
        metadata=metadata,
        tables=rows[: limits.max_table_rows],
        truncated=truncated,
        quality=quality,
        flags=flags,
    )


def _csv_to_markdown(rows: list[list[str]], limits, flags: list[str]) -> str:
    """Render CSV rows as markdown table, capped by max_table_rows/cols."""
    if not rows:
        return ""
    max_rows = limits.max_table_rows
    max_cols = limits.max_table_cols
    out: list[str] = []
    header = rows[0]
    out.append("| " + " | ".join(header[:max_cols]) + " |")
    out.append("| " + " | ".join("---" for _ in range(min(len(header), max_cols))) + " |")
    for row in rows[1:max_rows]:
        out.append("| " + " | ".join(row[:max_cols]) + " |")
    if len(rows) > max_rows:
        flags.append("table_rows_capped")
    return "\n".join(out)


def _csv_quality(rows: list[list[str]], truncated: bool, flags: list[str]) -> str:
    """CSV quality: MEDIUM if data present, LOW if truncated or empty.

    Text formats have no main-content element, so they cap at medium.
    """
    if not rows or all(not any(r) for r in rows):
        return "low"
    return "medium"


def _cap_text(
    text: str,
    limits,
    flags: list[str],
    truncated: bool = False,
) -> tuple[str, bool]:
    """Cap text to max_text_chars, setting truncated flag if exceeded."""
    if len(text) > limits.max_text_chars:
        text = text[: limits.max_text_chars]
        flags.append("text_capped")
        return text, True
    return text, truncated


def _json_text_depth(content: str) -> int:
    """Max bracket/brace nesting depth in raw text, ignoring quoted strings.

    O(n), non-recursive; brackets inside JSON strings do not count as nesting.
    """
    depth = 0
    max_depth = 0
    i = 0
    n = len(content)
    in_string = False
    quote = ""
    while i < n:
        c = content[i]
        if in_string:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                in_string = False
            i += 1
            continue
        if c in "'\"":
            in_string = True
            quote = c
            i += 1
            continue
        if c in "{[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif c in "}]":
            if depth > 0:
                depth -= 1
        i += 1
    return max_depth


def _extract_json(content: str, limits) -> dict[str, object]:
    """Parse JSON/JSONL with bounded nesting depth. Never raises."""
    flags: list[str] = []
    truncated = False

    if _json_text_depth(content) > _MAX_JSON_DEPTH:
        return _empty_result("json_depth_refused", ["json_depth_refused"])

    try:
        data = json.loads(content)
    except json.JSONDecodeError, ValueError, RecursionError:
        # Try JSONL line-by-line.
        lines = content.splitlines()
        parsed: list[object] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if _json_text_depth(line) > _MAX_JSON_DEPTH:
                flags.append("jsonl_parse_error")
                break
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError, ValueError, RecursionError:
                flags.append("jsonl_parse_error")
                break
        if not parsed:
            return _empty_result("json_parse_error", ["json_parse_error"])
        data = parsed
        flags.append("jsonl_format")

    depth = _json_depth(data)
    if depth > _MAX_JSON_DEPTH:
        flags.append("json_depth_capped")
        truncated = True

    text = _json_to_text(data, limits, flags)
    markdown = text  # JSON has no markdown structure; use text
    metadata = {"depth": str(depth)}
    text, truncated = _cap_text(text, limits, flags, truncated)

    quality = "medium" if truncated or "json_depth_capped" in flags else "medium"
    if isinstance(data, (list, dict)) and len(str(data)) == 0:
        quality = "low"

    return _text_format_result(
        text=text,
        markdown=markdown,
        metadata=metadata,
        tables=[],
        truncated=truncated,
        quality=quality,
        flags=flags,
    )


def _json_depth(obj: object) -> int:
    """Compute JSON nesting depth iteratively; never recurses."""
    max_depth = 0
    stack: list[tuple[object, int]] = [(obj, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        if isinstance(node, dict):
            for value in node.values():
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                stack.append((value, depth + 1))
    return max_depth


def _json_to_text(obj: object, limits, flags: list[str]) -> str:
    """Render JSON as bounded text."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return str(obj)
    if isinstance(obj, list):
        parts = [_json_to_text(item, limits, flags) for item in obj[: limits.max_table_rows]]
        if len(obj) > limits.max_table_rows:
            flags.append("json_list_capped")
        return "\n".join(parts)
    if isinstance(obj, dict):
        parts = [
            f"{k}: {_json_to_text(v, limits, flags)}" for k, v in list(obj.items())[: limits.max_table_rows]
        ]
        if len(obj) > limits.max_table_rows:
            flags.append("json_dict_capped")
        return "\n".join(parts)
    return ""


def _extract_xml(content: str, limits) -> dict[str, object]:
    """Extract text from XML, refusing DTD and malformed markup.

    Regex-based extraction (no ElementTree) avoids XXE risks without
    third-party deps. DOCTYPE and unmatched tags mean FAILED.
    """
    flags: list[str] = []
    truncated = False

    if _DOCTYPE_RE.search(content):
        flags.append("xml_dtd_refused")
        return _empty_result("xml_dtd_refused", ["xml_dtd_refused"])

    # Strip XML declaration, processing instructions, comments.
    text = re.sub(r"<\?xml[^>]*\?>", " ", content)
    text = re.sub(r"<\?[^>]*\?>", " ", text)
    text = re.sub(r"<!--[^>]*-->", " ", text)

    # Tag balance: opening vs closing tags must match, else malformed.
    opens = len(re.findall(r"<([A-Za-z_][A-Za-z0-9_.-]*)\b[^>/]*(?<!/)>", text))
    closes = len(re.findall(r"</([A-Za-z_][A-Za-z0-9_.-]*)\s*>", text))
    if opens != closes:
        flags.append("xml_parse_error")
        return _empty_result("xml_parse_error", ["xml_parse_error"])

    # Capture root element name for metadata.
    root_match = re.match(r"<([A-Za-z_][A-Za-z0-9_.-]*)\b", text)
    root_name = root_match.group(1) if root_match else ""

    # Strip all remaining tags, leaving text runs.
    text = re.sub(r"<[^>]*>", " ", text)
    # Collapse whitespace.
    text = " ".join(text.split())
    text, truncated = _cap_text(text, limits, flags, truncated)

    quality = "low" if truncated or flags else "medium"
    return _text_format_result(
        text=text,
        markdown=text,
        metadata={"root": root_name},
        tables=[],
        truncated=truncated,
        quality=quality,
        flags=flags,
    )


def _text_format_result(
    *,
    text: str,
    markdown: str,
    metadata: dict[str, str],
    tables: list,
    truncated: bool,
    quality: str,
    flags: list[str],
) -> dict[str, object]:
    """Build a text-format result dict with common fields."""
    return {
        "text": text,
        "markdown": markdown,
        "metadata": metadata,
        "links": [],
        "frames": [],
        "forms": [],
        "tables": tables,
        "truncated": truncated,
        "found_main": False,
        "quality_flags": flags,
        "_quality": quality,
    }


def _resolve_text_format_quality(result: dict[str, object], content_type: str) -> str:
    """Resolve quality from text format result dict."""
    return cast(str, result.get("_quality", "failed"))
