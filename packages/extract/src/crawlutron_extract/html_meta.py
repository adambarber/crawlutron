"""Metadata collector observed during the same streaming pass as the extractor.

Delegated handler calls from ``_HTMLExtractor``; one parse, no DOM.
Mirrors core's ``PageMetadata`` key set (see ``crawlutron.models.page_metadata``).
"""

from __future__ import annotations

import json

from safe_url import NormalizedUrl

__all__ = ["MetadataCollector"]

_OG_NAMES = {
    "og:title": "og_title",
    "og:description": "og_description",
    "og:image": "og_image",
    "og:url": "og_url",
    "og:type": "og_type",
    "og:site_name": "og_site_name",
}

_TWITTER_NAMES = {
    "twitter:card": "twitter_card",
    "twitter:title": "twitter_title",
    "twitter:description": "twitter_description",
    "twitter:image": "twitter_image",
}

_REL_FAVICON = frozenset({"icon", "shortcut icon", "apple-touch-icon"})


class MetadataCollector:
    """Collects ``<meta>``/``<link>``/``<script>`` metadata during one pass.

    ``base_url`` is the document's resolution base, refreshed on ``<base href>``.
    """

    def __init__(self, base_url: NormalizedUrl | None = None) -> None:
        self.base_url = base_url
        self._values: dict[str, str] = {}
        self._html_lang: str | None = None
        self._meta_lang: str | None = None
        self._json_ld: list[str] = []
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []
        self._hreflang: str | None = None
        self._canonical: str | None = None
        self._favicon: str | None = None
        self._title_parts: list[str] = []
        self._in_title = False

    def set_base_url(self, base_url: NormalizedUrl | None) -> None:
        self.base_url = base_url

    def handle_starttag(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "html":
            lang = (attrs.get("lang") or "").strip()
            if lang:
                self._html_lang = lang
        elif tag == "title":
            self._in_title = True
        elif tag == "link":
            self._handle_link(attrs)
        elif tag == "meta":
            self._handle_meta(attrs)
        elif tag == "script":
            if (attrs.get("type") or "").lower() == "application/ld+json":
                self._in_json_ld = True
                self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_json_ld:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            block = "".join(self._json_ld_parts).strip()
            if block:
                try:
                    json.loads(block)  # validate only; malformed JSON-LD is skipped
                except (ValueError, json.JSONDecodeError):
                    pass
                else:
                    self._json_ld.append(block)
            self._json_ld_parts = []

    def _resolve(self, href: str) -> str:
        if not href or self.base_url is None:
            return href
        resolved = self.base_url.resolve(href)
        return resolved._raw if resolved is not None else href

    def _handle_link(self, attrs: dict[str, str]) -> None:
        href = attrs.get("href") or ""
        if not href:
            return
        rel = (attrs.get("rel") or "").lower()
        if "canonical" in rel and self._canonical is None:
            self._canonical = href
        if any(token in rel for token in _REL_FAVICON) and self._favicon is None:
            self._favicon = href
        hreflang = (attrs.get("hreflang") or "").strip()
        if hreflang and self._hreflang is None:
            self._hreflang = hreflang

    def _handle_meta(self, attrs: dict[str, str]) -> None:
        content = attrs.get("content") or ""
        if not content:
            return
        name = (attrs.get("name") or attrs.get("property") or "").lower().strip()
        if not name:
            return
        mapped = _OG_NAMES.get(name) or _TWITTER_NAMES.get(name)
        if mapped is not None:
            self._values.setdefault(mapped, content)
            return
        if name == "description":
            self._values.setdefault("description", content)
        elif name == "author":
            self._values.setdefault("author", content)
        elif name == "charset":
            self._values.setdefault("charset", content)
        elif name == "lang":
            # <html lang> takes precedence; resolved in result().
            if self._meta_lang is None:
                self._meta_lang = content

    def set_title(self, title: str) -> None:
        self._values["title"] = title

    def merge_metadata(self, metadata: dict[str, str]) -> None:
        self._values.update(metadata)

    def result(self) -> dict[str, str]:
        metadata: dict[str, str] = dict(self._values)
        if self._html_lang:
            metadata["language"] = self._html_lang
        elif self._meta_lang and "language" not in metadata:
            metadata["language"] = self._meta_lang
        if self._canonical is not None:
            metadata["canonical_url"] = self._resolve(self._canonical)
        if self._favicon is not None:
            metadata["favicon"] = self._resolve(self._favicon)
        if self._hreflang is not None:
            metadata["hreflang"] = self._hreflang
        if len(self._json_ld) == 1:
            metadata["json_ld"] = self._json_ld[0]
        elif len(self._json_ld) > 1:
            metadata["json_ld"] = "\n".join(self._json_ld)
        title = " ".join("".join(self._title_parts).split())
        if title:
            metadata["title"] = title
        return metadata
