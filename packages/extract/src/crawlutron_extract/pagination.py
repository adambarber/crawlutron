"""Pagination detection: rel=next on <a> and <link>, resolved, non-http dropped."""

from __future__ import annotations

from html import parser as html_parser

from safe_url import HostPolicy, NormalizedUrl, admit_or_none

from crawlutron_extract._html_common import tag_attrs

__all__ = ["detect_pagination"]


def detect_pagination(html: bytes | str, final_url: str | NormalizedUrl | None) -> NormalizedUrl | None:
    """First rel=next href resolved against final_url; None when absent or non-http."""
    if not html or final_url is None:
        return None
    base = (
        final_url
        if isinstance(final_url, NormalizedUrl)
        else admit_or_none(final_url, policy=HostPolicy.unrestricted())
    )
    if base is None:
        return None
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    found: list[NormalizedUrl] = []

    class _P(html_parser.HTMLParser):
        def __init__(self, base_url: NormalizedUrl) -> None:
            super().__init__(convert_charrefs=True)
            self.base_url = base_url

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag not in ("a", "link"):
                return
            attr_dict = tag_attrs(attrs)
            rel = attr_dict.get("rel", "").lower()
            href = attr_dict.get("href", "").strip()
            if "next" not in rel.split():
                return
            if not href:
                return
            admitted = self.base_url.resolve(href)
            if admitted is not None:
                found.append(admitted)

    parser = _P(base)
    parser.feed(text)
    parser.close()
    return found[0] if found else None
