"""Text and markdown emission sink: whitespace collapsing, emphasis, inline
code, lists, blockquotes, images, <hr>.
"""

from __future__ import annotations

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LIST_TAGS = frozenset({"ul", "ol"})
_CODE_TAGS = frozenset({"pre", "code"})


class TextSink:
    """Collects plain-text and markdown parts during one parse pass."""

    def __init__(self, link_collector=None) -> None:
        self._text_parts: list[str] = []
        self._md_parts: list[str] = []
        self._title_parts: list[str] = []
        self._in_code = 0
        self._in_em = 0
        self._in_strong = 0
        self._in_blockquote = 0
        self._in_title = False
        self._list_stack: list[tuple[str, int]] = []
        self._link_collector = link_collector

    def handle_starttag(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "strong":
            self._open_strong()
        elif tag == "em":
            self._open_em()
        elif tag in _CODE_TAGS:
            self._open_code()
        elif tag == "blockquote":
            self._open_blockquote()
        elif tag == "img":
            self.append_md(f"![{attrs.get('alt', '')}]({attrs.get('src', '')})")
        elif tag == "hr":
            self.append_md("\n---\n")
        elif tag == "br":
            self.append_text("\n")
            self.append_md("\n")
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
            self.append_md(self._list_marker())

    def handle_endtag(self, tag: str) -> None:
        if tag == "strong":
            self._close_strong()
        elif tag == "em":
            self._close_em()
        elif tag in _CODE_TAGS:
            self._close_code()
        elif tag == "blockquote":
            self._close_blockquote()
        elif tag in _HEADING_TAGS:
            self.append_md("\n")
        elif tag == "p":
            self.append_md("\n")
        elif tag in _LIST_TAGS and self._list_stack:
            self._list_stack.pop()

    def handle_data(self, data: str) -> None:
        data = data.replace("\x00", "")
        if self._link_collector is not None and self._link_collector.is_in_link():
            return
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_code > 0:
            self._text_parts.append(data)
        else:
            self._text_parts.append(data)
        free_text = not self._in_title
        if free_text:
            self.append_md(data)

    def set_title_state(self, in_title: bool) -> None:
        self._in_title = in_title

    def _open_strong(self) -> None:
        self._in_strong += 1
        self.append_md("**")

    def _close_strong(self) -> None:
        self._in_strong = max(0, self._in_strong - 1)
        self.append_md("**")

    def _open_em(self) -> None:
        self._in_em += 1
        self.append_md("*")

    def _close_em(self) -> None:
        self._in_em = max(0, self._in_em - 1)
        self.append_md("*")

    def _open_code(self) -> None:
        self._in_code += 1
        self.append_md("`")

    def _close_code(self) -> None:
        self._in_code = max(0, self._in_code - 1)
        self.append_md("`")

    def _open_blockquote(self) -> None:
        self._in_blockquote += 1
        self.append_md("\n> ")

    def _close_blockquote(self) -> None:
        self._in_blockquote = max(0, self._in_blockquote - 1)
        self.append_md("\n")

    def _list_marker(self) -> str:
        depth = len(self._list_stack)
        indent = "  " * (depth - 1)
        if self._list_stack and self._list_stack[-1][0] == "ol":
            kind, count = self._list_stack[-1]
            self._list_stack[-1] = (kind, count + 1)
            return f"{indent}{count}. "
        return f"{indent}- "

    def append_text(self, text: str) -> None:
        self._text_parts.append(text)

    def append_md(self, text: str) -> None:
        if self._in_blockquote > 0:
            text = "".join("> " + line for line in text.splitlines(True))
        self._md_parts.append(text)

    def _emit(self, text: str) -> None:
        self.append_text(text)
        self.append_md(text)

    def result(self) -> tuple[str, str, str]:
        text = " ".join("".join(self._text_parts).split())
        markdown = "".join(self._md_parts)
        title = " ".join("".join(self._title_parts).split())
        return text, markdown, title
