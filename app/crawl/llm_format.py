"""LLM-friendly metadata extraction from HTML."""

import contextlib
import json
import re
from html import unescape
from typing import Any

from app.bounded_io import truncate_html_input

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


# Cap the HTML passed to lxml parsing for metadata extraction. lxml builds a
# full DOM tree; a 50 MB body can balloon to ~200 MB+ of RAM during parsing.
# Anything above a few MB is almost certainly above the practical meta-tag
# density and is rejected. The value lives in app.bounded_io; this site keeps
# the reason THIS parse needs one.


def _extract_llm_metadata(html: str, url: str) -> dict[str, Any]:
    """Extract Page metadata from HTML meta tags.

    # Parse the HTML once with lxml and read every field (title, meta
    og/twitter tags, JSON-LD, canonical, favicon, language) from the single
    tree instead of running 8+ independent regex scans over the raw string.
    Inputs above ``MAX_HTML_INPUT_BYTES`` are truncated before
    lxml parses them so a malicious oversized payload cannot exhaust RAM
    building the DOM tree.

    This is the fallback computation site: engine-produced items carry
    metadata computed once from the discovery tree
    (``app.crawl.extraction.extract_post_fetch_discovery``) and shaping
    reads that dict instead of reaching this function. Keep the two
    paths in sync through :func:`_llm_metadata_from_tree` -- it is the only
    reader of a parsed tree.
    """
    metadata: dict[str, Any] = {"source_url": url}
    html = truncate_html_input(html)
    # Lazy import avoids a circular import: extraction.py imports this module
    # at module level, so this direction must stay lazy.
    from app.crawl.extraction import _parse_html_tree

    tree = _parse_html_tree(html)
    if tree is None:
        return metadata
    return _llm_metadata_from_tree(tree, url)


def _llm_metadata_from_tree(tree: Any, url: str) -> dict[str, Any]:
    """Read the page-metadata fields from an ALREADY-parsed lxml tree.

    Shared by the fallback parse above and the engine's discovery stage, so
    the carried ``llm_metadata`` and a shaping-time reparse cannot drift
    into two different metadata dialects. Returns plain JSON data only --
    the tree itself never escapes the caller.
    """
    metadata: dict[str, Any] = {"source_url": url}

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
        with contextlib.suppress(Exception):  # malformed JSON-LD is skipped
            parsed_json_ld.append(json.loads(block))
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
        elif (
            any(k in rel for k in ("icon", "shortcut icon", "apple-touch-icon")) and "favicon" not in metadata
        ):
            metadata["favicon"] = href

    # language from <html lang=...>
    lang = tree.xpath("//html/@lang")
    if lang:
        metadata["language"] = lang[0].strip()
    elif tree.tag == "html" and tree.get("lang"):
        metadata["language"] = tree.get("lang").strip()

    return metadata


def text_from_html(html: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(
        r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", unescape(text)).strip()
