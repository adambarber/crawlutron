"""Table collection: header rows, caption, colspan/rowspan degradation.

Nested tables flatten into the enclosing cell; colspan/rowspan degrade the grid.
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
        self._nested_table_flattened = False
        self._caption_parts: list[str] = []
        self._in_caption = False
        self._truncated = False
        self._table_grid_degraded = False
        self._md_parts: list[str] = []
        # Pending rowspan carries: {(row_idx, col_idx): (remaining, text)}
        self._rowspan_pending: dict[tuple[int, int], tuple[int, str]] = {}
        # Columns occupied on each row by an active rowspan from an earlier row.
        self._occupied: dict[int, set[int]] = {}
        # Absolute row index of the row currently being built (increments once per
        # <tr>, independent of truncation, so columns never leak across rows).
        self._row_seq = 0
        self._current_row_idx = 0

    @property
    def in_table(self) -> int:
        return self._in_table

    def handle_starttag(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "table":
            self._start_table()
        elif tag == "tr":
            if self._in_table == 1:
                self._current_row = []
                self._current_row_idx = self._row_seq
                self._row_seq += 1
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
            if self._colspan > 1 or self._rowspan > 1:
                self._table_grid_degraded = True
        elif tag == "caption":
            self._in_caption = True

    def handle_data(self, data: str) -> None:
        if self._in_table < 1 or self._current_cell is None:
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
            # Nested table: flatten into the enclosing cell instead of refusing.
            self._nested_table_flattened = True
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
        # Only the outermost table close finalizes the table; a nested table
        # close just pops the nesting depth and leaves the outer table intact.
        if self._in_table == 0:
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
        # Apply pending rowspans to this row before appending.
        self._apply_rowspan_spans()
        self._current_table.append(self._current_row)
        if self._current_row_header and len(self._current_table) == 1:
            self._table_first_row_header = True
        self._current_row = None
        self._current_row_header = False
        # Decrement rowspan counters after appending the row.
        self._decrement_rowspan()

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
        colspan = max(1, self._colspan)
        rowspan = max(1, self._rowspan)
        # Place the cell at the first column not occupied by an active rowspan
        # carried down from an earlier row (keeps the grid rectangular).
        row_idx = self._current_row_idx
        occupied = self._occupied.get(row_idx, set())
        col = 0
        while col in occupied:
            col += 1
        while len(self._current_row) <= col + colspan - 1:
            self._current_row.append("")
        for col_offset in range(colspan):
            self._current_row[col + col_offset] = cell_text
        for col_offset in range(colspan):
            self._occupied.setdefault(row_idx, set()).add(col + col_offset)
        if rowspan > 1:
            self._table_grid_degraded = True
            for target in range(row_idx + 1, row_idx + rowspan):
                self._occupied.setdefault(target, set()).update(range(col, col + colspan))
            self._rowspan_pending[(row_idx, col)] = (rowspan - 1, cell_text)
        self._current_cell = None
        self._current_cell_kind = None
        self._colspan = 1
        self._rowspan = 1

    def _apply_rowspan_spans(self) -> None:
        """Fill pending rowspan cells into the row about to be closed.

        Carries text down spanned rows; exhausted carries are dropped.
        """
        if not self._current_row:
            return
        if self._current_table is None:
            return
        row_idx = len(self._current_table)
        pending: dict[tuple[int, int], tuple[int, str]] = {}
        for (r, c), (remaining, text) in self._rowspan_pending.items():
            if r < row_idx and remaining > 0:
                # Due on this row: carry the text down into column c.
                while len(self._current_row) <= c:
                    self._current_row.append("")
                self._current_row[c] = text
                if remaining - 1 > 0:
                    pending[(r, c)] = (remaining - 1, text)
                # else exhausted: drop it.
            elif remaining > 0:
                # Not due yet (same row) or still has rows left; keep it.
                pending[(r, c)] = (remaining, text)
            # else exhausted: drop it.
        self._rowspan_pending = pending

    def _decrement_rowspan(self) -> None:
        """Rowspan counters are consumed at apply time; kept as a no-op hook."""

    def _render_table_markdown(self, table: list[list[str]], has_header: bool) -> None:
        if self._caption_parts:
            self._md_parts.append(" ".join("".join(self._caption_parts).split()))
        for ri, row in enumerate(table):
            self._md_parts.append(f"| {' | '.join(row)} |")
            if ri == 0 and has_header:
                self._md_parts.append("|" + "|".join("---" for _ in row) + "|")

    def result(self) -> tuple[list[list[list[str]]], list[bool], int, bool]:
        flags: int = 0
        if self._nested_table_flattened:
            flags |= 1
        if self._table_grid_degraded:
            flags |= 2
        return self._tables, self._table_headers, flags, self._truncated

    def markdown(self) -> str:
        return "".join(self._md_parts)
