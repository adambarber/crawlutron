"""Sitemap locs: urlset page locs and sitemapindex child sitemaps as their own list."""

from __future__ import annotations

import re

from crawlutron_extract._html_common import resolve_or_admit
from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.text_format import _DOCTYPE_RE
from safe_url import NormalizedUrl

__all__ = ["extract_sitemap_locs"]

#: Entity declarations share the DTD refusal of the existing XML path.
_ENTITY_RE = re.compile(r"<!ENTITY\s", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_OPEN_RE = re.compile(r"<sitemap\b[^>]*>", re.IGNORECASE)
_SITEMAP_CLOSE_RE = re.compile(r"</sitemap\s*>", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def extract_sitemap_locs(
    xml: bytes | str,
    *,
    limits: ExtractLimits | None = None,
) -> tuple[list[NormalizedUrl], list[NormalizedUrl]]:
    """Page locs and child sitemap URLs, each in first-seen order, deduped and capped.

    A DTD or entity declaration refuses the whole document, matching the
    existing XML path. Relative locs are refused without a source URL.
    """
    if limits is None:
        limits = ExtractLimits()
    if isinstance(xml, bytes):
        if len(xml) > limits.max_input_bytes:
            return [], []
        text = xml.decode("utf-8", "replace")
    else:
        if len(xml.encode("utf-8")) > limits.max_input_bytes:
            return [], []
        text = xml
    if _DOCTYPE_RE.search(text) or _ENTITY_RE.search(text):
        return [], []
    page_locs: list[NormalizedUrl] = []
    sitemap_locs: list[NormalizedUrl] = []
    seen: set[str] = set()
    max_links = limits.max_links
    # Walk sitemap open/close tags and <loc> matches in document order, tracking
    # sitemap nesting depth as one counter so each byte is examined once.
    events: list[tuple[int, str]] = []
    loc_matches: list[re.Match[str]] = []
    for m in _SITEMAP_OPEN_RE.finditer(text):
        events.append((m.start(), "open"))
    for m in _SITEMAP_CLOSE_RE.finditer(text):
        events.append((m.start(), "close"))
    for m in _LOC_RE.finditer(text):
        events.append((m.start(), "loc"))
        loc_matches.append(m)
    events.sort(key=lambda e: e[0])
    depth = 0
    loc_idx = 0
    for _pos, kind in events:
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
        else:
            m = loc_matches[loc_idx]
            loc_idx += 1
            raw = _WHITESPACE_RE.sub(" ", m.group(1)).strip()
            if not raw:
                continue
            admitted = resolve_or_admit(raw, None)
            if admitted is None or admitted.raw in seen:
                continue
            seen.add(admitted.raw)
            if len(page_locs) + len(sitemap_locs) >= max_links:
                return page_locs, sitemap_locs
            (sitemap_locs if depth > 0 else page_locs).append(admitted)
    return page_locs, sitemap_locs
