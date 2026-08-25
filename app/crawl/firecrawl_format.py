"""Firecrawl-compatible metadata extraction from HTML."""

import json
import re
from html import unescape
from typing import Any


_META_MAP = {
    "description": "description",
    "og:title": "og_title",
    "og:description": "og_description",
    "og:image": "og_image",
    "og:url": "og_url",
    "og:type": "og_type",
    "og:site_name": "og_site_name",
    "twitter:card": "twitter_card",
    "twitter:title": "twitter_title",
    "twitter:description": "twitter_description",
    "twitter:image": "twitter_image",
}


def _extract_firecrawl_metadata(html: str, url: str) -> dict[str, Any]:
    """Extract Firecrawl-style metadata from HTML meta tags.

    #15: parse the HTML once with lxml and read every field (title, meta
    og/twitter tags, JSON-LD, canonical, favicon, language) from the single
    tree instead of running 8+ independent regex scans over the raw string.
    """
    metadata: dict[str, Any] = {"source_url": url}
    # Lazy import avoids a circular import: extraction.py imports this module.
    from app.crawl.extraction import _parse_html_tree

    tree = _parse_html_tree(html)
    if tree is None:
        return metadata

    # title
    title = tree.findtext(".//title")
    if title:
        metadata["title"] = re.sub(r"\s+", " ", title).strip()

    # meta tags (name / property -> content)
    for el in tree.iter("meta"):
        name = (el.get("name") or el.get("property") or "").lower().strip()
        if not name:
            continue
        value = (el.get("content") or "").strip()
        if not value:
            continue
        mapped = _META_MAP.get(name)
        if mapped:
            metadata[mapped] = value

    # JSON-LD structured data
    parsed_json_ld: list[Any] = []
    for script in tree.iter("script"):
        if (script.get("type") or "").lower() != "application/ld+json":
            continue
        block = script.text
        if not block:
            continue
        try:
            parsed_json_ld.append(json.loads(block))
        except Exception:  # malformed JSON-LD is skipped
            continue
    if parsed_json_ld:
        metadata["json_ld"] = parsed_json_ld[0] if len(parsed_json_ld) == 1 else parsed_json_ld

    # canonical URL + favicon from <link rel=...>
    for el in tree.iter("link"):
        rel = (el.get("rel") or "").lower()
        href = (el.get("href") or "").strip()
        if not href:
            continue
        if "canonical" in rel and "canonical_url" not in metadata:
            metadata["canonical_url"] = href
        elif any(k in rel for k in ("icon", "shortcut icon", "apple-touch-icon")) and "favicon" not in metadata:
            metadata["favicon"] = href

    # language from <html lang=...>
    lang = tree.xpath("//html/@lang")
    if lang:
        metadata["language"] = lang[0].strip()
    elif tree.tag == "html" and tree.get("lang"):
        metadata["language"] = tree.get("lang").strip()

    return metadata


def _text_from_html(html: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", unescape(text)).strip()
