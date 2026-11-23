"""HTML single-pass extraction: text, markdown, metadata, links, frames, forms.

html.parser streaming: memory O(output); delegates collection to collaborators.
"""

from __future__ import annotations

from html import parser as html_parser
from typing import TypedDict

from crawlutron_extract.html_forms_frames import FormFrameCollector
from crawlutron_extract.html_links import LinkCollector
from crawlutron_extract.html_meta import MetadataCollector
from crawlutron_extract.html_tables import TableCollector
from crawlutron_extract.html_text import TextSink
from crawlutron_extract.limits import ExtractLimits
from safe_url import HostPolicy, NormalizedUrl

__all__ = ["extract_html"]

_BOILERPLATE_TAGS = frozenset({"nav", "aside", "footer", "script", "style"})
_ROLE_BOILERPLATE = frozenset({"banner", "navigation", "search", "complementary", "contentinfo"})
_MAIN_TAGS = frozenset({"article", "main"})


def _role_is_boilerplate(attrs: dict[str, str]) -> bool:
    return any(token in _ROLE_BOILERPLATE for token in attrs.get("role", "").split())


class _HtmlResult(TypedDict):
    text: str
    markdown: str
    metadata: dict[str, str]
    links: list[NormalizedUrl]
    frames: list[dict]
    forms: list[dict]
    tables: list[list[list[str]]]
    truncated: bool
    found_main: bool
    links_refused: int
    flags: list[str]


def extract_html(
    content: str,
    limits: ExtractLimits,
    base_url: str | None = None,
    policy: HostPolicy | None = None,
) -> _HtmlResult:
    extractor = _HTMLExtractor(limits, base_url=base_url, policy=policy)
    extractor.feed(content)
    extractor.close()
    return extractor.result()


class _HTMLExtractor(html_parser.HTMLParser):
    def __init__(self, limits, base_url=None, policy=None, _srcdoc_depth=0) -> None:
        super().__init__(convert_charrefs=True)
        self._limits = limits
        self._stack: list[dict] = []
        self._in_main = 0
        self._boilerplate_depth = 0
        self._found_main = False
        self._policy = policy
        self._base_url = None
        if base_url is not None and policy is not None:
            try:
                self._base_url = NormalizedUrl(base_url, policy=policy)
            except ValueError:
                pass
        self._meta = MetadataCollector(self._base_url)
        self._links = LinkCollector(limits, policy)
        self._links.set_base_url(self._base_url)
        self._tables = TableCollector(limits)
        self._forms_frames = FormFrameCollector(limits)
        self._text = TextSink(self._links)
        self._srcdoc_depth = _srcdoc_depth
        self._srcdoc_refused = False
        self._truncated = False
        self._links_truncated = False

    @property
    def _in_boilerplate(self) -> bool:
        return self._boilerplate_depth > 0

    def handle_starttag(self, tag, attrs) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        is_boilerplate = tag in _BOILERPLATE_TAGS or _role_is_boilerplate(attr_dict)
        is_main = tag in _MAIN_TAGS or "main" in attr_dict.get("role", "").split()
        self._stack.append({"tag": tag, "boilerplate": is_boilerplate, "main": is_main})
        if is_boilerplate:
            self._boilerplate_depth += 1
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
                self._meta.set_base_url(self._base_url)
                self._links.set_base_url(self._base_url)
        if tag == "a":
            self._links.handle_starttag(tag, attr_dict)
        elif tag in ("iframe", "frame"):
            self._handle_frame_start(attr_dict)
        elif tag in ("form", "input", "button", "select", "option", "textarea"):
            self._forms_frames.handle_starttag(tag, attr_dict)
        self._meta.handle_starttag(tag, attr_dict)
        if self._in_boilerplate:
            return
        self._text.handle_starttag(tag, attr_dict)
        self._tables.handle_starttag(tag, attr_dict)

    def _handle_frame_start(self, attrs: dict[str, str]) -> None:
        srcdoc = attrs.get("srcdoc", "")
        frame_entry: dict = {
            "src": attrs.get("src", ""),
            "srcdoc": srcdoc,
            "name": attrs.get("name", ""),
            "sandbox": attrs.get("sandbox", ""),
        }
        if srcdoc and self._srcdoc_depth < 5:
            self._srcdoc_depth += 1
            nested = _HTMLExtractor(
                self._limits,
                base_url=self._base_url._raw if self._base_url is not None else None,
                policy=self._policy,
                _srcdoc_depth=self._srcdoc_depth,
            )
            nested.feed(srcdoc)
            nested.close()
            nr = nested.result()
            self._text.append_text(nr["text"])
            self._text.append_md(nr["markdown"])
            for link in nr["links"]:
                self._links.add_link(link)
            self._links.add_links_refused(nr["links_refused"])
            self._forms_frames.add_frames(nr["frames"])
            self._meta.merge_metadata(nr["metadata"])
            if "srcdoc_depth_refused" in nr["flags"]:
                self._truncated = True
                self._srcdoc_refused = True
            self._srcdoc_depth -= 1
        elif srcdoc:
            self._truncated = True
            self._srcdoc_refused = True
        self._forms_frames.add_frame(frame_entry)

    def handle_endtag(self, tag) -> None:
        if not self._stack:
            return
        frame = self._stack.pop()
        if frame["boilerplate"]:
            self._boilerplate_depth -= 1
        if frame["main"]:
            self._in_main -= 1
        if tag == "a":
            self._links.handle_endtag(tag)
        self._meta.handle_endtag(tag)
        if self._in_boilerplate:
            return
        self._text.handle_endtag(tag)
        self._tables.handle_endtag(tag)
        self._forms_frames.handle_endtag(tag)

    def handle_data(self, data) -> None:
        self._meta.handle_data(data)
        self._links.handle_data(data)
        data = data.replace("\x00", "")
        if self._in_boilerplate and self._in_main == 0:
            return
        if self._tables.in_table <= 1:
            self._text.handle_data(data)
        self._tables.handle_data(data)
        self._forms_frames.handle_data(data)

    def result(self) -> _HtmlResult:
        text, markdown, title = self._text.result()
        if title:
            self._meta.set_title(title)
        tables, table_headers, table_flags, tables_truncated = self._tables.result()
        forms, frames, ff_flags = self._forms_frames.result()
        links, links_refused = self._links.result()
        metadata = self._meta.result()
        truncated = self._srcdoc_refused or tables_truncated or self._links._links_truncated
        table_md = self._tables.markdown()
        markdown = markdown + table_md
        flags: list[str] = []
        if self._srcdoc_refused:
            flags.append("srcdoc_depth_refused")
        if table_flags & 1:
            flags.append("nested_table_flattened")
        if table_flags & 2:
            flags.append("table_grid_degraded")
        if len(text) > self._limits.max_text_chars:
            text = text[: self._limits.max_text_chars]
            truncated = True
        return {
            "text": text,
            "markdown": markdown[: self._limits.max_markdown_chars],
            "metadata": metadata,
            "links": links,
            "frames": frames,
            "forms": forms,
            "tables": tables,
            "truncated": truncated,
            "found_main": self._found_main,
            "links_refused": links_refused,
            "flags": flags,
        }
