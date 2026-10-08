"""LLM-friendly metadata extraction from HTML."""

from __future__ import annotations

import contextlib
import json
import re
from html import unescape
from typing import Any, cast, get_args, get_type_hints

from app.bounded_io import truncate_html_input
from app.models.normalized_url import NormalizedUrl
from app.models.page_metadata import PageMetadata

#: Every field's declared type, read from the record rather than transcribed, so
#: a field added there is admitted here without a second edit. A bare ``str`` has
#: no union members -- ``get_args(str)`` is empty and the hint IS the type -- so
#: the bare case is kept whole and only ``None`` is stripped from a union,
#: because ``total=False`` means absence is legal and admitting ``None`` would
#: make every check pass.
_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {}
for _field, _hint in get_type_hints(PageMetadata).items():
    _members = get_args(_hint)
    if not _members:
        _FIELD_TYPES[_field] = _hint
        continue
    _live = tuple(arg for arg in _members if arg is not type(None))
    _FIELD_TYPES[_field] = _live[0] if len(_live) == 1 else _live


def extract_carried_metadata(value: Any) -> PageMetadata:
    """The one place unproven page metadata becomes :class:`PageMetadata`.

    ``llm_metadata`` arrives from the engine spill, which is JSON: untyped,
    mutable, and possibly written by a different build than the one reading it.
    It is also arbitrary-shaped, so it is admitted field by field rather than
    rejected whole -- a payload with four good fields and one junk one is worth
    answering. A key that is not a declared field, or whose value does not match
    the declared type, is dropped; everything else survives.

    This is the checkpoint the type system cannot be for us. ``metadata[mapped]
    = value`` is unchecked, and a dict read back from JSON is exactly the thing
    the annotations describe without being, so once :func:`format_crawl_item`
    claims to return :class:`PageMetadata`, this is the only line standing
    between the claim and the truth.
    """
    if not isinstance(value, dict):
        return {}
    admitted = {
        key: item
        for key, item in value.items()
        if (expected := _FIELD_TYPES.get(key)) is not None and isinstance(item, expected)
    }
    return cast(PageMetadata, admitted)


# Cap the HTML passed to lxml parsing for metadata extraction. lxml builds a
# full DOM tree; a 50 MB body can balloon to ~200 MB+ of RAM during parsing.
# Anything above a few MB is almost certainly above the practical meta-tag
# density and is rejected. The value lives in app.bounded_io; this site keeps
# the reason THIS parse needs one.


def extract_llm_metadata(html: str, url: NormalizedUrl | None) -> PageMetadata:
    """Extract Page metadata from HTML meta tags.

    # Parse the HTML once with lxml and read every field (title, meta
    og/twitter tags, JSON-LD, canonical, favicon, language) from the single
    tree instead of running 8+ independent regex scans over the raw string.
    Inputs above ``MAX_HTML_INPUT_BYTES`` are truncated before
    lxml parses them so a malicious oversized payload cannot exhaust RAM
    building the DOM tree.

    This is the fallback computation site: engine-produced items carry
    metadata computed once from the discovery tree
    (``app.extraction.content.extract_post_fetch_discovery``) and shaping
    reads that dict instead of reaching this function. Keep the two
    paths in sync through :func:`_llm_metadata_from_tree` -- it is the only
    reader of a parsed tree.
    """
    source = url.raw if url is not None else ""
    metadata: PageMetadata = {"source_url": source}
    html = truncate_html_input(html)
    # Lazy import avoids a circular import: extraction.py imports this module
    # at module level, so this direction must stay lazy.
    from app.extraction.content import parse_html_tree

    tree = parse_html_tree(html)
    if tree is None:
        return metadata
    return _llm_metadata_from_tree(tree, url)


def _llm_metadata_from_tree(tree: Any, url: NormalizedUrl | None) -> PageMetadata:
    """Read the page-metadata fields from an ALREADY-parsed lxml tree.

    Shared by the fallback parse above and the engine's discovery stage, so
    the carried ``llm_metadata`` and a shaping-time reparse cannot drift
    into two different metadata dialects. Returns plain JSON data only --
    the tree itself never escapes the caller.
    """
    source = url.raw if url is not None else ""
    metadata: PageMetadata = {"source_url": source}
    _carry_meta_tags(metadata, tree)

    # title
    title = tree.findtext(".//title")
    if title:
        metadata["title"] = re.sub(r"\s+", " ", title).strip()

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


def _carry_meta_tags(metadata: PageMetadata, tree: Any) -> None:
    """Carry the ``<meta>`` tags that name a declared field into `metadata`.

    One literal-key assignment per HTML meta name rather than a rename table
    applied with a computed key. Every type checker this project runs rejects a
    computed key into a TypedDict -- mypy calls it "expected TypedDict key to be
    string literal", and pyright types the target as one field's type -- so the
    table made the destination field unverifiable, and a table that renames
    ``og:title`` to ``og_title`` had to be held in step with the record by a
    separate detector. Spelling the pairs out here costs that table and gives
    back a checked destination: a mistyped field name is a build failure rather
    than a value that silently never reaches the wire. It is also the whole
    list, so an HTML name no case lists is simply not carried.
    """
    # meta tags (name / property -> content)
    for el in tree.iter("meta"):
        name = (el.get("name") or el.get("property") or "").lower().strip()
        if not name:
            continue
        value = (el.get("content") or "").strip()
        if not value:
            continue
        match name:
            case "description":
                metadata["description"] = value
            case "og:title":
                metadata["og_title"] = value
            case "og:description":
                metadata["og_description"] = value
            case "og:image":
                metadata["og_image"] = value
            case "og:url":
                metadata["og_url"] = value
            case "og:type":
                metadata["og_type"] = value
            case "og:site_name":
                metadata["og_site_name"] = value
            case "twitter:card":
                metadata["twitter_card"] = value
            case "twitter:title":
                metadata["twitter_title"] = value
            case "twitter:description":
                metadata["twitter_description"] = value
            case "twitter:image":
                metadata["twitter_image"] = value

    return None


def text_from_html(html: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(
        r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", unescape(text)).strip()


__all__ = [
    "PageMetadata",
    "extract_carried_metadata",
    "extract_llm_metadata",
    "text_from_html",
]
