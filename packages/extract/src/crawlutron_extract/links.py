"""Discovered links: base href, safe_url admission, dedup, cap, refusals."""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron_extract._html_common import _HtmlParser, _prepare, resolve_or_admit, tag_attrs
from crawlutron_extract.limits import ExtractLimits
from safe_url import NormalizedUrl

__all__ = ["LinkRecord", "extract_discovered_links"]

_DROP_SCHEMES = ("#", "mailto:", "tel:", "javascript:", "data:")


@dataclass(frozen=True)
class LinkRecord:
    """One admitted discovered link and what the caller may queue from it."""

    normalized_url: NormalizedUrl
    raw_href: str
    text: str
    rel: tuple[str, ...]
    is_same_host: bool


def extract_discovered_links(
    html: bytes | str,
    source_url: str | NormalizedUrl | None,
    *,
    limits: ExtractLimits | None = None,
) -> tuple[list[LinkRecord], int]:
    """Admitted links and the refusal count, capped after admission."""
    text, base, limits = _prepare(html, source_url, limits)
    source_host = base.host if base is not None else ""
    links: list[LinkRecord] = []
    seen: set[str] = set()
    refused = 0
    max_links = limits.max_links
    current_base = base
    in_link = False
    link_url = ""
    link_rel: tuple[str, ...] = ()
    link_text_parts: list[str] = []

    class _P(_HtmlParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            nonlocal current_base, in_link, link_url, link_rel, link_text_parts
            attr_dict = tag_attrs(attrs)
            if tag == "base":
                href = attr_dict.get("href", "").strip()
                if href:
                    target = current_base or base
                    admitted = resolve_or_admit(href, target)
                    if admitted is not None:
                        current_base = admitted
                return
            if tag != "a":
                return
            in_link = True
            link_url = attr_dict.get("href", "").strip()
            link_rel = tuple(attr_dict.get("rel", "").split())
            link_text_parts = []

        def handle_endtag(self, tag: str) -> None:
            nonlocal in_link, link_url, link_rel, link_text_parts, refused
            if tag != "a" or not in_link:
                return
            in_link = False
            href = link_url
            link_url = ""
            rel = link_rel
            link_rel = ()
            body = " ".join("".join(link_text_parts).split())
            link_text_parts = []
            if not href and not body:
                return
            if href.startswith(_DROP_SCHEMES):
                refused += 1
                return
            target = current_base or base
            admitted = resolve_or_admit(href, target)
            if admitted is None:
                refused += 1
                return
            if admitted.raw in seen:
                return
            seen.add(admitted.raw)
            if len(links) >= max_links:
                return
            links.append(
                LinkRecord(
                    normalized_url=admitted,
                    raw_href=href,
                    text=body,
                    rel=rel,
                    is_same_host=admitted.host == source_host,
                )
            )

        def handle_data(self, data: str) -> None:
            if in_link:
                link_text_parts.append(data)

    parser = _P()
    parser.feed(text)
    parser.close()
    return links, refused
