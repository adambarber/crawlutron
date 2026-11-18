"""HTML single-pass extraction: text, markdown, metadata, links, frames, forms.

html.parser streaming: memory O(output), not O(document).
"""

from __future__ import annotations

from html import parser as html_parser
from typing import TypedDict

from safe_url import HostPolicy, NormalizedUrl, admit_or_none

__all__ = ["extract_html"]

_BOILERPLATE_TAGS = frozenset({"nav", "aside", "footer", "script", "style"})
_ROLE_BOILERPLATE = frozenset({"banner", "navigation", "search", "complementary", "contentinfo"})
_MAIN_TAGS = frozenset({"article", "main"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LIST_TAGS = frozenset({"ul", "ol"})
_CODE_TAGS = frozenset({"pre", "code"})


class _HtmlResult(TypedDict):
    text: str
    markdown: str
    metadata: dict[str, str]
    links: list[NormalizedUrl]
    frames: list[_FrameEntry]
    forms: list[_FormEntry]
    tables: list[list[list[str]]]
    truncated: bool
    found_main: bool
    links_refused: int
    flags: list[str]


class _FrameEntry(TypedDict):
    src: str
    srcdoc: str
    name: str


class _FormEntry(TypedDict):
    action: str
    method: str
    fields: list[dict[str, str]]


def _role_is_boilerplate(attrs: dict[str, str]) -> bool:
    return any(token in _ROLE_BOILERPLATE for token in attrs.get("role", "").split())


def _is_main(tag: str, attrs: dict[str, str]) -> bool:
    return tag in _MAIN_TAGS or "main" in attrs.get("role", "").split()


def _collapse_whitespace(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def extract_html(
    content: str,
    limits,
    base_url: str | None = None,
    policy: HostPolicy | None = None,
) -> _HtmlResult:
    extractor = _HTMLExtractor(limits, base_url=base_url, policy=policy)
    extractor.feed(content)
    extractor.close()
    return extractor.result()


class _HTMLExtractor(html_parser.HTMLParser):
    def __init__(self, limits, base_url=None, policy=None) -> None:
        super().__init__(convert_charrefs=True)
        self._limits = limits
        self._stack: list[dict[str, object]] = []
        self._text_parts: list[str] = []
        self._md_parts: list[str] = []
        self._main_parts: list[str] = []
        self._links: list[NormalizedUrl] = []
        self._seen_links: set[str] = set()
        self._links_refused = 0
        self._frames: list[_FrameEntry] = []
        self._forms: list[_FormEntry] = []
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
        self._in_main = 0
        self._found_main = False
        self._in_code = 0
        self._in_em = 0
        self._in_strong = 0
        self._in_blockquote = 0
        self._in_link = False
        self._in_title = False
        self._link_url = ""
        self._link_text_parts: list[str] = []
        self._title_parts: list[str] = []
        self._metadata: dict[str, str] = {}
        self._truncated = False
        self._list_stack: list[tuple[str, int]] = []
        self._policy = policy
        self._base_url = None
        if base_url is not None and policy is not None:
            try:
                self._base_url = NormalizedUrl(base_url, policy=policy)
            except ValueError:
                pass

    @property
    def _in_boilerplate(self) -> bool:
        return any(f["boilerplate"] for f in self._stack)

    def _append_text(self, text: str) -> None:
        (self._main_parts if self._in_main > 0 else self._text_parts).append(text)

    def _append_md(self, text: str) -> None:
        if self._in_blockquote > 0:
            text = "".join("> " + line for line in text.splitlines(True))
        self._md_parts.append(text)

    def _emit(self, text: str) -> None:
        self._append_text(text)
        self._append_md(text)

    def _open_strong(self) -> None:
        self._in_strong += 1
        self._append_md("**")

    def _close_strong(self) -> None:
        self._in_strong = max(0, self._in_strong - 1)
        self._append_md("**")

    def _open_em(self) -> None:
        self._in_em += 1
        self._append_md("*")

    def _close_em(self) -> None:
        self._in_em = max(0, self._in_em - 1)
        self._append_md("*")

    def _open_code(self) -> None:
        self._in_code += 1
        self._append_md("`")

    def _close_code(self) -> None:
        self._in_code = max(0, self._in_code - 1)
        self._append_md("`")

    def _open_blockquote(self) -> None:
        self._in_blockquote += 1
        self._append_md("\n> ")

    def _close_blockquote(self) -> None:
        self._in_blockquote = max(0, self._in_blockquote - 1)
        self._append_md("\n")

    def _list_marker(self) -> str:
        depth = len(self._list_stack)
        indent = "  " * (depth - 1)
        if self._list_stack and self._list_stack[-1][0] == "ol":
            kind, count = self._list_stack[-1]
            self._list_stack[-1] = (kind, count + 1)
            return f"{indent}{count}. "
        return f"{indent}- "

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
        cell_text = _collapse_whitespace("".join(self._current_cell))
        if self._current_cell_kind == "th":
            self._current_row_header = True
        for _ in range(max(1, self._colspan)):
            self._current_row.append(cell_text)
        self._current_cell = None
        self._current_cell_kind = None
        self._colspan = 1
        self._rowspan = 1

    def _close_link(self) -> None:
        if not self._in_link:
            return
        self._in_link = False
        text = _collapse_whitespace("".join(self._link_text_parts))
        if not text and not self._link_url:
            self._link_url = ""
            self._link_text_parts = []
            return
        resolved_url = self._link_url
        if self._base_url is not None:
            resolved = self._base_url.resolve(self._link_url)
            if resolved is not None:
                resolved_url = resolved._raw
        admitted = admit_or_none(resolved_url, policy=self._policy) if self._policy is not None else None
        if admitted is None:
            self._links_refused += 1
        elif admitted.raw not in self._seen_links:
            self._seen_links.add(admitted.raw)
            if len(self._links) >= self._limits.max_links:
                self._truncated = True
            else:
                self._links.append(admitted)
        self._link_url = ""
        self._link_text_parts = []

    def _table_markdown(self) -> str:
        if not self._tables:
            return ""
        lines: list[str] = []
        for table, has_header in zip(self._tables, self._table_headers, strict=False):
            if self._caption_parts:
                lines.append(_collapse_whitespace("".join(self._caption_parts)))
            for ri, row in enumerate(table):
                lines.append(f"| {' | '.join(row)} |")
                if ri == 0 and has_header:
                    lines.append("|" + "|".join("---" for _ in row) + "|")
        return "\n".join(lines)

    def result(self) -> _HtmlResult:
        body = self._main_parts if self._main_parts else self._text_parts
        text = _collapse_whitespace("".join(body))
        if self._title_parts:
            self._metadata.setdefault("title", _collapse_whitespace("".join(self._title_parts)))
        markdown = self._build_markdown()
        if self._nested_table_refused:
            self._truncated = True
        if len(text) > self._limits.max_text_chars:
            text = text[: self._limits.max_text_chars]
            self._truncated = True
        if len(markdown) > self._limits.max_markdown_chars:
            markdown = markdown[: self._limits.max_markdown_chars]
            self._truncated = True
        html_flags: list[str] = []
        if self._nested_table_refused:
            html_flags.append("nested_table_refused")
        return {
            "text": text,
            "markdown": markdown,
            "metadata": self._metadata,
            "links": self._links,
            "frames": self._frames,
            "forms": self._forms,
            "tables": self._tables,
            "truncated": self._truncated,
            "found_main": self._found_main,
            "links_refused": self._links_refused,
            "flags": html_flags,
        }

    def _build_markdown(self) -> str:
        parts = list(self._md_parts)
        if self._tables:
            parts.append(self._table_markdown())
        return "".join(parts)[: self._limits.max_markdown_chars]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        is_boilerplate = tag in _BOILERPLATE_TAGS or _role_is_boilerplate(attr_dict)
        is_main = _is_main(tag, attr_dict)
        self._stack.append({"tag": tag, "boilerplate": is_boilerplate, "main": is_main})
        if is_main:
            self._in_main += 1
            self._found_main = True

        if tag == "base" and attr_dict.get("href") and self._policy is not None:
            href = attr_dict["href"]
            resolved = self._base_url.resolve(href) if self._base_url is not None else None
            if resolved is None:
                try:
                    resolved = NormalizedUrl(href, policy=self._policy)
                except ValueError:
                    resolved = None
            if resolved is not None:
                self._base_url = resolved

        if tag == "a":
            self._in_link = True
            self._link_url = attr_dict.get("href", "")
            self._link_text_parts = []
        elif tag in ("iframe", "frame"):
            self._add_frame(attr_dict)
        elif tag == "form":
            self._handle_form_start(attr_dict)
        elif tag in ("input", "button", "select", "textarea"):
            self._handle_form_field(tag, attr_dict)

        if self._in_boilerplate:
            return
        self._handle_flow_start(tag, attr_dict)

    def _handle_flow_start(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._handle_meta(attrs)
        elif tag == "strong":
            self._open_strong()
        elif tag == "em":
            self._open_em()
        elif tag in _CODE_TAGS:
            self._open_code()
        elif tag == "blockquote":
            self._open_blockquote()
        elif tag == "img":
            self._append_md(f"![{attrs.get('alt', '')}]({attrs.get('src', '')})")
        elif tag == "hr":
            self._append_md("\n---\n")
        elif tag == "table":
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
        elif tag == "br":
            self._emit("\n")
        elif tag in _HEADING_TAGS:
            self._emit("#" * int(tag[1]) + " ")
        elif tag in _LIST_TAGS:
            start = 1
            if tag == "ol":
                try:
                    start = max(0, int(attrs.get("start", "1") or "1"))
                except ValueError:
                    start = 1
            self._list_stack.append((tag, start))
        elif tag == "li":
            self._append_md(self._list_marker())

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        frame = self._stack.pop()
        if frame["main"]:
            self._in_main -= 1
        if tag == "a":
            self._close_link()
        if self._in_boilerplate:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "strong":
            self._close_strong()
        elif tag == "em":
            self._close_em()
        elif tag in _CODE_TAGS:
            self._close_code()
        elif tag == "blockquote":
            self._close_blockquote()
        elif tag == "table":
            self._close_table()
        elif tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "caption":
            self._in_caption = False
        elif tag in _HEADING_TAGS:
            self._append_md("\n")
        elif tag == "p":
            self._append_md("\n")
        elif tag in _LIST_TAGS and self._list_stack:
            self._list_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_boilerplate and self._in_main == 0:
            return
        if self._in_table > 1:
            return
        data = data.replace("\x00", "")
        target = self._main_parts if self._in_main > 0 else self._text_parts
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_link:
            self._link_text_parts.append(data)
        elif self._in_caption:
            self._caption_parts.append(data)
        elif self._current_cell is not None:
            self._current_cell.append(data)
        elif self._in_code > 0:
            target.append(data)
        else:
            target.append(data)
        free_text = not (
            self._in_link or self._in_caption or self._current_cell is not None or self._in_title
        )
        if free_text:
            self._append_md(data)

    def _add_frame(self, attrs: dict[str, str]) -> None:
        if len(self._frames) >= self._limits.max_frames:
            self._truncated = True
            return
        self._frames.append(
            {"src": attrs.get("src", ""), "srcdoc": attrs.get("srcdoc", ""), "name": attrs.get("name", "")}
        )

    def _handle_meta(self, attrs: dict[str, str]) -> None:
        if not attrs.get("content"):
            return
        name = (attrs.get("name") or attrs.get("property") or "").lower()
        if name in ("title", "description", "author", "lang") or name.startswith("og:"):
            self._metadata[name] = attrs["content"]
        if name == "charset":
            self._metadata["charset"] = attrs["content"]

    def _handle_form_start(self, attrs: dict[str, str]) -> None:
        if len(self._forms) >= self._limits.max_forms:
            self._truncated = True
            return
        self._forms.append(
            {"action": attrs.get("action", ""), "method": attrs.get("method", "GET").upper(), "fields": []}
        )

    def _handle_form_field(self, tag: str, attrs: dict[str, str]) -> None:
        if not self._forms or len(self._forms[-1]["fields"]) >= self._limits.max_links:
            return
        self._forms[-1]["fields"].append(
            {"type": attrs.get("type", ""), "name": attrs.get("name", ""), "value": attrs.get("value", "")}
        )
