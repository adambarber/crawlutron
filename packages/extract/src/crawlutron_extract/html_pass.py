"""HTML single-pass extraction: text, markdown, metadata, links, frames, forms.

html.parser streaming: memory O(output), not O(document). Boilerplate skipped;
article/main/[role=main] preferred. Links admitted through safe_url (never raw strings).
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


class _FrameEntry(TypedDict):
    src: str
    srcdoc: str
    name: str


class _FormEntry(TypedDict):
    action: str
    method: str
    fields: list[dict[str, str]]


def _role_is_boilerplate(attrs: dict[str, str]) -> bool:
    role = attrs.get("role", "")
    return any(token in _ROLE_BOILERPLATE for token in role.split())


def _is_main(tag: str, attrs: dict[str, str]) -> bool:
    if tag in _MAIN_TAGS:
        return True
    return "main" in attrs.get("role", "").split()


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs, preserve newlines."""
    lines = text.split("\n")
    return "\n".join(" ".join(line.split()) for line in lines)


def extract_html(
    content: str,
    limits,
    base_url: str | None = None,
    policy: HostPolicy | None = None,
) -> _HtmlResult:
    """Extract from HTML in one pass; never raises."""
    extractor = _HTMLExtractor(limits, base_url=base_url, policy=policy)
    extractor.feed(content)
    extractor.close()
    return extractor.result()


class _HTMLExtractor(html_parser.HTMLParser):
    """Event-based parser producing bounded markdown and metadata."""

    def __init__(self, limits, base_url: str | None = None, policy: HostPolicy | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._limits = limits
        self._stack: list[dict[str, object]] = []
        self._text_parts: list[str] = []
        self._main_parts: list[str] = []
        self._links: list[NormalizedUrl] = []
        self._seen_links: set[str] = set()
        self._links_refused = 0
        self._frames: list[_FrameEntry] = []
        self._forms: list[_FormEntry] = []
        self._tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_main = 0
        self._found_main = False
        self._in_code = 0
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
        """True if any ancestor frame is boilerplate. Stack-based so unclosed
        boilerplate tags in real-world HTML cannot permanently silence text."""
        return any(f["boilerplate"] for f in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        is_boilerplate = tag in _BOILERPLATE_TAGS or _role_is_boilerplate(attr_dict)
        is_main = _is_main(tag, attr_dict)
        self._stack.append({"tag": tag, "boilerplate": is_boilerplate, "main": is_main})
        if is_main:
            self._in_main += 1
            self._found_main = True

        # <base href> changes the resolution base for relative URLs.
        if tag == "base" and attr_dict.get("href") and self._policy is not None:
            href = attr_dict["href"]
            if self._base_url is not None:
                resolved = self._base_url.resolve(href)
            else:
                try:
                    resolved = NormalizedUrl(href, policy=self._policy)
                except ValueError:
                    resolved = None
            if resolved is not None:
                self._base_url = resolved

        # Links/frames/forms are collected document-wide (used for frontier
        # discovery), not gated behind main-content or boilerplate selection.
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
        if tag == "title":
            self._in_title = True
        self._handle_start_specific(tag, attr_dict)

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        frame = self._stack.pop()
        if frame["main"]:
            self._in_main -= 1

        # Close links document-wide so link metadata is always recorded.
        if tag == "a":
            self._close_link()

        if self._in_boilerplate:
            return
        if tag == "title":
            self._in_title = False
        self._handle_end_specific(tag)

    def handle_data(self, data: str) -> None:
        # Skip text inside boilerplate unless inside main-content (Wikipedia
        # nests <main> inside boilerplate wrappers; the content must extract).
        if self._in_boilerplate and self._in_main == 0:
            return
        data = data.replace("\x00", "")
        target = self._main_parts if self._in_main > 0 else self._text_parts
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_link:
            self._link_text_parts.append(data)
        elif self._in_code > 0:
            target.append(data)
        elif self._current_cell is not None:
            self._current_cell.append(data)
        else:
            target.append(data)

    def _handle_start_specific(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "meta":
            self._handle_meta(attrs)
        elif tag == "table":
            self._start_table()
        elif tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._current_cell = []
        elif tag == "br":
            self._append_text("\n")
        elif tag in _CODE_TAGS:
            self._in_code += 1
        elif tag in _HEADING_TAGS:
            self._append_text("#" * int(tag[1]) + " ")
        elif tag in _LIST_TAGS:
            if tag == "ol":
                self._list_stack.append(("ol", 1))
            else:
                self._list_stack.append(("ul", 0))
        elif tag == "li":
            marker = self._list_marker()
            self._append_text(marker)

    def _handle_end_specific(self, tag: str) -> None:
        if tag == "table":
            self._close_table()
        elif tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in _CODE_TAGS:
            self._in_code = max(0, self._in_code - 1)
        elif tag in _LIST_TAGS:
            self._list_stack.pop()
        elif tag in _HEADING_TAGS:
            self._append_text("\n")
        elif tag == "p":
            self._append_text("\n")

    def _handle_meta(self, attrs: dict[str, str]) -> None:
        if not attrs.get("content"):
            return
        name = (attrs.get("name") or attrs.get("property") or "").lower()
        if name in ("title", "description", "author", "lang") or name.startswith("og:"):
            self._metadata[name] = attrs["content"]
        if name == "charset":
            self._metadata["charset"] = attrs["content"]

    def _add_frame(self, attrs: dict[str, str]) -> None:
        if len(self._frames) >= self._limits.max_frames:
            self._truncated = True
            return
        frame_entry: _FrameEntry = {
            "src": attrs.get("src", ""),
            "srcdoc": attrs.get("srcdoc", ""),
            "name": attrs.get("name", ""),
        }
        self._frames.append(frame_entry)

    def _handle_form_start(self, attrs: dict[str, str]) -> None:
        if len(self._forms) >= self._limits.max_forms:
            self._truncated = True
            return
        form_entry: _FormEntry = {
            "action": attrs.get("action", ""),
            "method": attrs.get("method", "GET").upper(),
            "fields": [],
        }
        self._forms.append(form_entry)

    def _handle_form_field(self, tag: str, attrs: dict[str, str]) -> None:
        if not self._forms or len(self._forms[-1]["fields"]) >= self._limits.max_links:
            return
        self._forms[-1]["fields"].append(
            {
                "type": attrs.get("type", ""),
                "name": attrs.get("name", ""),
                "value": attrs.get("value", ""),
            }
        )

    def _start_table(self) -> None:
        if len(self._tables) >= self._limits.max_tables:
            self._truncated = True
            return
        self._current_table = []

    def _close_table(self) -> None:
        if self._current_table is not None:
            self._tables.append(self._current_table)
        self._current_table = None

    def _close_row(self) -> None:
        if self._current_table is None or self._current_row is None:
            return
        if len(self._current_table) >= self._limits.max_table_rows:
            self._truncated = True
            self._current_row = None
            return
        self._current_table.append(self._current_row)
        self._current_row = None

    def _close_cell(self) -> None:
        if self._current_row is None or self._current_cell is None:
            return
        if len(self._current_row) >= self._limits.max_table_cols:
            self._truncated = True
            self._current_cell = None
            return
        self._current_row.append(_collapse_whitespace("".join(self._current_cell)))
        self._current_cell = None

    def _close_link(self) -> None:
        if not self._in_link:
            return
        self._in_link = False
        text = _collapse_whitespace("".join(self._link_text_parts))
        if not text and not self._link_url:
            self._link_url = ""
            self._link_text_parts = []
            return
        # Resolve against base URL, admit through safe_url, dedupe, cap after admission.
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

    def _list_marker(self) -> str:
        if self._list_stack and self._list_stack[-1][0] == "ol":
            kind, count = self._list_stack[-1]
            self._list_stack[-1] = (kind, count + 1)
            return f"{count}. "
        return "- "

    def _append_text(self, text: str) -> None:
        target = self._main_parts if self._in_main > 0 else self._text_parts
        target.append(text)

    def result(self) -> _HtmlResult:
        body = self._main_parts if self._main_parts else self._text_parts
        text = _collapse_whitespace("".join(body))
        if self._title_parts:
            self._metadata.setdefault("title", _collapse_whitespace("".join(self._title_parts)))
        markdown = self._build_markdown()
        if len(text) > self._limits.max_text_chars:
            text = text[: self._limits.max_text_chars]
            self._truncated = True
        if len(markdown) > self._limits.max_markdown_chars:
            markdown = markdown[: self._limits.max_markdown_chars]
            self._truncated = True
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
        }

    def _build_markdown(self) -> str:
        return "".join(self._text_parts)[: self._limits.max_markdown_chars]
