"""Extraction size caps: every list output is capped and carries truncated."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ExtractLimits"]


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
