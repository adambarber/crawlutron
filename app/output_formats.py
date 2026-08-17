"""#17 Multiple output formats for crawl results.

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
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

SUPPORTED_OUTPUT_FORMATS: tuple[str, ...] = ("json", "jsonl", "ndjson", "markdown", "csv")


def _default(obj: Any) -> Any:
    """JSON fallback for objects that are not natively serializable."""
    reducer = getattr(obj, "model_dump", None)
    if callable(reducer):
        return reducer()
    reducer = getattr(obj, "dict", None)
    if callable(reducer):
        return reducer()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


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


def _to_csv(results: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        writer.writerow({c: _safe(r.get(c)) for c in _CSV_COLUMNS})
    return buf.getvalue()
