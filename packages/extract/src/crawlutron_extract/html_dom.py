"""Bounded DOM builder for schema-driven extraction.

Hard caps on node count and depth; streaming fast path is the default.
"""

from __future__ import annotations

from html import parser as html_parser


class DOMNode:
    """A node in the bounded extraction DOM."""

    def __init__(self, tag: str, attrs: dict[str, str], parent: DOMNode | None = None) -> None:
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list[DOMNode] = []
        self._text = ""

    def add_text(self, text: str) -> None:
        self._text += text

    def add_child(self, child: DOMNode) -> None:
        self.children.append(child)

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def get_text(self) -> str:
        """All descendant text concatenated."""
        parts = [self._text]
        for child in self.children:
            parts.append(child.get_text())
        return "".join(parts)


class DOMBuilder(html_parser.HTMLParser):
    """Builds a bounded DOM tree from HTML using html.parser."""

    def __init__(self, max_nodes: int, max_depth: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.root: DOMNode | None = None
        self._stack: list[DOMNode] = []
        self._node_count = 0
        self._truncated = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._truncated:
            return
        if self._node_count >= self.max_nodes:
            self._truncated = True
            return
        new_depth = len(self._stack) + 1
        if new_depth > self.max_depth:
            self._truncated = True
            return
        attr_dict = {k: (v or "") for k, v in attrs}
        node = DOMNode(tag, attr_dict, self._stack[-1] if self._stack else None)
        if self._stack:
            self._stack[-1].add_child(node)
        else:
            self.root = node
        self._stack.append(node)
        self._node_count += 1

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._truncated:
            return
        if self._stack:
            self._stack[-1].add_text(data)

    def result(self) -> tuple[DOMNode | None, bool]:
        if self.root is None:
            return None, self._truncated
        if len(self._stack) > 0:
            # Multiple top-level elements: wrap in synthetic root
            synthetic = DOMNode("root", {}, None)
            for root_child in self._stack:
                synthetic.add_child(root_child)
            return synthetic, self._truncated
        return self.root, self._truncated


def build_dom(content: str, max_nodes: int, max_depth: int) -> tuple[DOMNode | None, bool]:
    """Build a bounded DOM tree from HTML.

    Full documents parse as-is; fragments wrap so <html>/<body> never duplicate.
    """
    builder = DOMBuilder(max_nodes, max_depth)
    full = _is_full_document(content)
    if full:
        builder.feed(content)
    else:
        builder.feed("<html><body>" + content + "</body></html>")
    builder.close()
    root, truncated = builder.result()
    # Wrapped fragments expose their <body> root; full documents keep their real
    # <html> root so html/body selectors match exactly once.
    if not full and root is not None and root.tag == "html" and root.children:
        root = root.children[0]
    return root, truncated


def _is_full_document(content: str) -> bool:
    """True when ``content`` already carries its own <html> document root."""
    stripped = content.lstrip()
    return stripped.lower().startswith(("<html", "<!doctype"))
