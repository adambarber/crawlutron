"""LLM-friendly metadata extraction from HTML."""

from __future__ import annotations

import contextlib
import json
import re
from html import unescape
from typing import Any, cast, get_args, get_type_hints

from app.extraction.classify import strip_html_noise
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl
from app.models.page_metadata import PageMetadata
from app.safety.bounded_io import truncate_html_input

#: a field added there is admitted here without a second edit; only None is stripped
#: from a union, and the system-type field resolves to the JSON union's membership.
_JSON_MEMBERSHIP = (type(None), bool, int, float, str, list, dict)
_WHITESPACE_RE = re.compile(r"\s+")
_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {}
for _field, _hint in get_type_hints(PageMetadata).items():
    if _hint is JsonValue:
        _FIELD_TYPES[_field] = _JSON_MEMBERSHIP
        continue
    _members = get_args(_hint)
    if not _members:
        _FIELD_TYPES[_field] = _hint
        continue
    _live = tuple(arg for arg in _members if arg is not type(None))
    _FIELD_TYPES[_field] = _live[0] if len(_live) == 1 else _live


def extract_carried_metadata(value: Any) -> PageMetadata:
    """The one place unproven page metadata becomes :class:`PageMetadata`: the JSON
    from the engine spill is admitted field by field (a junk field drops, the rest
    survives) -- the only line standing between the typed return and the untyped dict.
    """
    if not isinstance(value, dict):
        return {}
    admitted = {
        key: item
        for key, item in value.items()
        if (expected := _FIELD_TYPES.get(key)) is not None and isinstance(item, expected)
    }
    return cast(PageMetadata, admitted)


# Cap the HTML passed to lxml parsing for metadata extraction: a 50 MB body can balloon
# to ~200 MB+ of RAM. The value lives in app.safety.bounded_io; this site keeps the why.


def extract_llm_metadata(html: str, url: NormalizedUrl | None) -> PageMetadata:
    """Extract Page metadata from HTML meta tags off ONE lxml parse (not 8+ regex
    scans), truncating oversized input first. This is the fallback computation
    site -- engine items carry it already -- kept in sync via ``_llm_metadata_from_tree``.
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
    return llm_metadata_from_tree(tree, url)


def llm_metadata_from_tree(tree: Any, url: NormalizedUrl | None) -> PageMetadata:
    """Read the page-metadata fields from an ALREADY-parsed lxml tree, shared by the
    fallback and the engine's discovery stage so the two cannot drift into different
    dialects; returns plain JSON data, the tree never escaping the caller.
    """
    source = url.raw if url is not None else ""
    metadata: PageMetadata = {"source_url": source}
    _carry_meta_tags(metadata, tree)

    # title
    title = tree.findtext(".//title")
    if title:
        metadata["title"] = re.sub(r"\s+", " ", title).strip()

    # JSON-LD structured data -- arbitrary third-party JSON: the system type, not
    # ``Any``; malformed blocks are skipped.
    parsed_json_ld: list[JsonValue] = []
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
    """Carry the ``<meta>`` tags that name a declared field into ``metadata``: one
    literal-key assignment per tag, not a rename table -- checkers reject a computed
    key into a TypedDict, making a mistyped field a build error, not a lost value.
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
    # The linear, input-capped noise scanner classify owns (#440): the lazy-quantifier
    # regexes this replaced went quadratic on unterminated <script floods.
    return _WHITESPACE_RE.sub(" ", unescape(strip_html_noise(html))).strip()


__all__ = [
    "PageMetadata",
    "extract_carried_metadata",
    "extract_llm_metadata",
    "text_from_html",
]
