"""Extraction size caps: every list output is capped and carries truncated."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ExtractLimits", "RICH_TEXT_FLOOR"]

#: Minimum extracted body text length for quality="high" (main-content
#: element found, no lossy decode). Below this floor, quality caps at "medium".
RICH_TEXT_FLOOR: int = 200


def _base_result() -> dict:
    """Skeleton of the extraction result dict: every list output starts empty."""
    return {
        "links": [],
        "frames": [],
        "forms": [],
        "tables": [],
        "truncated": False,
        "found_main": False,
        "quality_flags": [],
    }


def _empty_result(reason: str, flags: list[str] | None = None) -> dict:
    """Shared empty extraction result: every field present, quality=failed."""
    result = _base_result()
    result.update(
        {
            "text": "",
            "markdown": "",
            "metadata": {"refusal": reason},
            "quality_flags": flags or ["format_refused"],
        }
    )
    return result


@dataclass(frozen=True)
class ExtractLimits:
    """Caps applied to every extraction; input above max_input_bytes is refused."""

    max_text_chars: int = 100_000
    max_markdown_chars: int = 200_000
    max_links: int = 1_000
    max_frames: int = 100
    max_forms: int = 100
    max_tables: int = 50
    max_table_rows: int = 1_000
    max_table_cols: int = 100
    max_input_bytes: int = 50_000_000
    max_dom_nodes: int = 50_000
    max_dom_depth: int = 50
    max_records: int = 10_000
    max_fields_per_record: int = 100
    max_zip_entries: int = 10_000
    max_zip_ratio: int = 1000
    max_zip_member_bytes: int = 1_000_000
    max_zip_inflated_bytes: int = 10_000_000
    max_pdf_pages: int = 1000
    max_pdf_inflated_bytes: int = 10_000_000
