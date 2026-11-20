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
    text = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
    if _DOCTYPE_RE.search(text) or _ENTITY_RE.search(text):
        return [], []
    page_locs: list[NormalizedUrl] = []
    sitemap_locs: list[NormalizedUrl] = []
    seen: set[str] = set()
    max_links = limits.max_links
    for match in _LOC_RE.finditer(text):
        raw = _WHITESPACE_RE.sub(" ", match.group(1)).strip()
        if not raw:
            continue
        prefix = text[: match.start()]
        inside = _count(_SITEMAP_OPEN_RE, prefix) > _count(_SITEMAP_CLOSE_RE, prefix)
        admitted = resolve_or_admit(raw, None)
        if admitted is None or admitted.raw in seen:
            continue
        seen.add(admitted.raw)
        if len(page_locs) + len(sitemap_locs) >= max_links:
            return page_locs, sitemap_locs
        (sitemap_locs if inside else page_locs).append(admitted)
    return page_locs, sitemap_locs


def _count(pattern: re.Pattern[str], text: str) -> int:
    return len(pattern.findall(text))
