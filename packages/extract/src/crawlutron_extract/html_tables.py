"""Table collection: header rows, caption, colspan/rowspan degradation.

Collected document-wide. Nested tables are refused with a flag, never crash.
Cell boundaries are lost (flattened) - a stated limitation.
"""

from __future__ import annotations

from crawlutron_extract.limits import ExtractLimits


class TableCollector:
    """Collects tables during one pass."""

    def __init__(self, limits: ExtractLimits) -> None:
        self._limits = limits
        self._tables: list[list[list[str]]] = []
        self._table_headers: list[bool] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._current_cell_kind: str | None = None
        self._current_row_header = False
        self._table_first_row_header = False
        self._colspan = 1
        self._rowspan = 1
        self._in_table = 0
        self._nested_table_refused = False
        self._caption_parts: list[str] = []
        self._in_caption = False
        self._truncated = False
        self._md_parts: list[str] = []

    @property
    def in_table(self) -> int:
        return self._in_table

    def handle_starttag(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "table":
            self._start_table()
        elif tag == "tr":
            if self._in_table == 1:
                self._current_row = []
        elif tag in ("td", "th"):
            if self._in_table == 1:
                self._current_cell = []
                self._current_cell_kind = tag
            try:
                self._colspan = max(1, int(attrs.get("colspan", "1") or "1"))
            except ValueError:
                self._colspan = 1
            try:
                self._rowspan = max(1, int(attrs.get("rowspan", "1") or "1"))
            except ValueError:
                self._rowspan = 1
        elif tag == "caption":
            self._in_caption = True

    def handle_data(self, data: str) -> None:
        if self._in_table != 1 or self._current_cell is None:
            return
        self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._close_table()
        elif tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "caption":
            self._in_caption = False

    def _start_table(self) -> None:
        if self._in_table > 0:
            self._nested_table_refused = True
            self._in_table += 1
            return
        if len(self._tables) >= self._limits.max_tables:
            self._truncated = True
            return
        self._in_table = 1
        self._current_table = []
        self._table_first_row_header = False
        self._caption_parts = []

    def _close_table(self) -> None:
        if self._in_table <= 0 or self._current_table is None:
            return
        self._in_table -= 1
        if self._nested_table_refused:
            self._current_table = None
            self._current_row = None
            self._current_cell = None
            return
        if self._current_table:
            self._tables.append(self._current_table)
            self._table_headers.append(self._table_first_row_header)
            self._render_table_markdown(self._current_table, self._table_first_row_header)
        self._current_table = None

    def _close_row(self) -> None:
        if self._in_table != 1 or self._current_table is None or self._current_row is None:
            return
        if len(self._current_table) >= self._limits.max_table_rows:
            self._truncated = True
            self._current_row = None
            return
        self._current_table.append(self._current_row)
        if self._current_row_header and len(self._current_table) == 1:
            self._table_first_row_header = True
        self._current_row = None
        self._current_row_header = False

    def _close_cell(self) -> None:
        if self._in_table != 1 or self._current_row is None or self._current_cell is None:
            return
        if len(self._current_row) >= self._limits.max_table_cols:
            self._truncated = True
            self._current_cell = None
            self._current_cell_kind = None
            return
        cell_text = " ".join("".join(self._current_cell).split())
        if self._current_cell_kind == "th":
            self._current_row_header = True
        for _ in range(max(1, self._colspan)):
            self._current_row.append(cell_text)
        self._current_cell = None
        self._current_cell_kind = None
        self._colspan = 1
        self._rowspan = 1

    def _render_table_markdown(self, table: list[list[str]], has_header: bool) -> None:
        if self._caption_parts:
            self._md_parts.append(" ".join("".join(self._caption_parts).split()))
        for ri, row in enumerate(table):
            self._md_parts.append(f"| {' | '.join(row)} |")
            if ri == 0 and has_header:
                self._md_parts.append("|" + "|".join("---" for _ in row) + "|")

    def result(self) -> tuple[list[list[list[str]]], list[bool], int, bool]:
        flags: int = 0
        if self._nested_table_refused:
            flags |= 1
        return self._tables, self._table_headers, flags, self._truncated

    def markdown(self) -> str:
        return "".join(self._md_parts)
