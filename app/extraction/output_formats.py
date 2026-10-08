"""Multiple output formats for crawl results.

The unified crawl result is a list of dicts (one per crawled URL). This module
serializes that list into the caller-requested wire format without changing the
underlying data shape:

* ``json``     -> pretty-printed JSON array (default; the API model response)
* ``jsonl``    -> one JSON object per line
* ``ndjson``   -> newline-delimited JSON (same as jsonl, stream-friendly)
* ``markdown`` -> one Markdown section per result (LLM-friendly)
* ``csv``      -> flat tabular view of the most useful columns

All serializers are pure and JSON-safe (pydantic models and arbitrary objects
are reduced to dicts/strings via ``_default``).

This module owns the vocabulary. ``OutputFormat`` is the only place the set is
written out -- ``Literal`` cannot be built from a runtime tuple -- and
``SUPPORTED_OUTPUT_FORMATS`` below is derived from it, so the annotation the
model enforces and the set this module dispatches on cannot drift apart. The
serializer's if/elif chain is asserted against the same set.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Literal, get_args

#: The formats a caller may name. Spelled out here because ``Literal`` requires
#: literal members, and kept here because this is the module that has to
#: dispatch on them. Everything else derives from it.
OutputFormat = Literal["json", "jsonl", "ndjson", "markdown", "csv"]

#: Derived from the annotation above rather than written beside it. Two
#: hand-kept copies of one set is the drift this project rejects elsewhere, and
#: it was the state this module was in: the serializer's membership test and the
#: model's annotation could disagree, and nothing tested that they did.
SUPPORTED_OUTPUT_FORMATS: tuple[str, ...] = get_args(OutputFormat)


def _default(obj: Any) -> Any:
    """Output fallback -- now the shared app.safety.json_safe chokepoint."""
    from app.safety.json_safe import json_default

    return json_default(obj)


def _safe(val: Any) -> str:
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False, default=_default)
    return "" if val is None else str(val)


def serialize_results(results: list[dict[str, Any]], fmt: str | None) -> tuple[str, str]:
    """Return ``(body, media_type)`` for ``results`` in the requested ``fmt``.

    Raises ``ValueError("unsupported_output_format")`` for unknown formats.
    """
    fmt = (fmt or "json").lower()
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(f"unsupported_output_format: {fmt}")
    if fmt == "json":
        return (
            json.dumps(results, ensure_ascii=False, indent=2, default=_default),
            "application/json",
        )
    if fmt in ("jsonl", "ndjson"):
        lines = [json.dumps(r, ensure_ascii=False, default=_default) for r in results]
        return "\n".join(lines), "application/x-ndjson"
    if fmt == "markdown":
        return _to_markdown(results), "text/markdown; charset=utf-8"
    if fmt == "csv":
        return _to_csv(results), "text/csv; charset=utf-8"
    raise ValueError(f"unsupported_output_format: {fmt}")  # pragma: no cover


def _to_markdown(results: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for r in results:
        url = r.get("final_url") or r.get("normalized_url") or r.get("original_url") or "unknown"
        lines = [f"# {_safe(url)}", ""]
        lines.append(f"- status: {_safe(r.get('status'))}")
        lines.append(f"- classification: {_safe(r.get('classification'))}")
        lines.append(f"- selected_strategy: {_safe(r.get('selected_strategy'))}")
        lines.append(f"- quality_score: {_safe(r.get('quality_score'))}")
        if r.get("error"):
            lines.append(f"- error: {_safe(r.get('error'))}")
        md = r.get("markdown")
        if md:
            lines.append("")
            lines.append("## Content")
            lines.append("")
            lines.append(_safe(md))
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(r, ensure_ascii=False, indent=2, default=_default))
        lines.append("```")
        lines.append("")
        sections.append("\n".join(lines))
    return "\n".join(sections)


_CSV_COLUMNS = [
    "original_url",
    "normalized_url",
    "final_url",
    "status",
    "classification",
    "selected_strategy",
    "quality_score",
    "discovered_link_count",
    "error",
    "http_status",
]

# CSV/Excel formula injection guard (#csv-injection). Spreadsheet apps execute
# cells whose first character is one of ``= + - @`` (or TAB / CR) as a
# formula. Crawled page content is attacker-influenced; a malicious page
# whose title is ``=cmd|'/c calc'!A1`` would otherwise execute when the
# caller opens the CSV. Prefixing with a single quote neutralizes the
# formula while staying readable (most spreadsheets drop the leading quote
# on display). URLs / numeric columns are exempt: ``=`` in a URL is just a
# query separator.
_CSV_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
_CSV_NEUTRALIZE_COLUMNS: set[str] = {"error", "classification", "status"}


def _csv_neutralize(value: str, column: str) -> str:
    """Escape a CSV cell that might trigger formula execution in a spreadsheet.

    Only columns that flow attacker-controlled page content are neutralized
    (per-column allowlist); URLs and numeric columns are returned unchanged
    so existing analyses still work.
    """
    if column not in _CSV_NEUTRALIZE_COLUMNS or not value:
        return value
    if value[0] in _CSV_FORMULA_PREFIXES:
        # Leading single-quote neutralizes formulas in Excel/Sheets/Numbers
        # without changing display.
        return "'" + value
    return value


def _to_csv(results: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        row: dict[str, str] = {}
        for c in _CSV_COLUMNS:
            value = _safe(r.get(c))
            row[c] = _csv_neutralize(value, c)
        writer.writerow(row)
    return buf.getvalue()
