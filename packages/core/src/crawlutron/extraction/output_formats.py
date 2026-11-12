"""Multiple output formats for crawl results.

Serializes the unified list-of-dicts result into wire formats (json/jsonl/ndjson/markdown/csv);
``OutputFormat`` is the only place the set is written, so annotation and dispatch cannot drift.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from typing import Literal, get_args

from crawlutron.models.json_value import JsonValue

#: The formats a caller may name. ``Literal`` needs literal members, and this
#: module dispatches on them; everything else derives from this set.
OutputFormat = Literal["json", "jsonl", "ndjson", "markdown", "csv"]

#: Derived from the annotation above: a second hand-kept copy of one set is
#: drift nothing tests, and it is what this module used to carry.
SUPPORTED_OUTPUT_FORMATS: tuple[str, ...] = get_args(OutputFormat)


def jsonify(obj: object) -> object:
    """Output fallback -- now the shared crawlutron.safety.json_safe chokepoint."""
    from crawlutron.safety.json_safe import json_default

    return json_default(obj)


def safe_value(val: object) -> str:
    if isinstance(val, Mapping):
        return json.dumps(val, ensure_ascii=False, default=jsonify)
    if isinstance(val, list):
        return json.dumps(val, ensure_ascii=False, default=jsonify)
    return "" if val is None else str(val)


def serialize_results(results: list[dict[str, JsonValue]], fmt: str | None) -> tuple[str, str]:
    """Return ``(body, media_type)`` for ``results`` in the requested ``fmt``.

    Raises ``ValueError("unsupported_output_format")`` for unknown formats.
    """
    fmt = (fmt or "json").lower()
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(f"unsupported_output_format: {fmt}")
    if fmt == "json":
        return (
            json.dumps(results, ensure_ascii=False, indent=2, default=jsonify),
            "application/json",
        )
    if fmt in ("jsonl", "ndjson"):
        lines = [json.dumps(r, ensure_ascii=False, default=jsonify) for r in results]
        return "\n".join(lines), "application/x-ndjson"
    if fmt == "markdown":
        return _to_markdown(results), "text/markdown; charset=utf-8"
    if fmt == "csv":
        return _to_csv(results), "text/csv; charset=utf-8"
    raise ValueError(f"unsupported_output_format: {fmt}")  # pragma: no cover


def _to_markdown(results: list[dict[str, JsonValue]]) -> str:
    sections: list[str] = []
    for r in results:
        url = r.get("final_url") or r.get("normalized_url") or r.get("original_url") or "unknown"
        lines = [f"# {safe_value(url)}", ""]
        lines.append(f"- status: {safe_value(r.get('status'))}")
        lines.append(f"- classification: {safe_value(r.get('classification'))}")
        lines.append(f"- selected_strategy: {safe_value(r.get('selected_strategy'))}")
        lines.append(f"- quality_score: {safe_value(r.get('quality_score'))}")
        if r.get("error"):
            lines.append(f"- error: {safe_value(r.get('error'))}")
        md = r.get("markdown")
        if md:
            lines.append("")
            lines.append("## Content")
            lines.append("")
            lines.append(safe_value(md))
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(r, ensure_ascii=False, indent=2, default=jsonify))
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

# CSV/Excel formula injection guard (#csv-injection): spreadsheets execute cells
# starting with one of ``= + - @`` (or TAB / CR); a leading quote neutralizes them.
_CSV_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
CSV_NEUTRALIZE_COLUMNS: set[str] = {"error", "classification", "status"}


def csv_neutralize(value: str, column: str) -> str:
    """Escape a CSV cell that might trigger formula execution in a spreadsheet.

    Only columns flowing attacker-controlled page content are neutralized (per-column
    allowlist); URLs and numeric columns stay unchanged so existing analyses work.
    """
    if column not in CSV_NEUTRALIZE_COLUMNS or not value:
        return value
    if value[0] in _CSV_FORMULA_PREFIXES:
        # Leading single-quote neutralizes formulas in Excel/Sheets/Numbers
        # without changing display.
        return "'" + value
    return value


def _to_csv(results: list[dict[str, JsonValue]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        row: dict[str, str] = {}
        for c in _CSV_COLUMNS:
            value = safe_value(r.get(c))
            row[c] = csv_neutralize(value, c)
        writer.writerow(row)
    return buf.getvalue()
