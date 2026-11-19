"""LLM-friendly metadata extraction from HTML via ``crawlutron_extract.extract``."""

from __future__ import annotations

import contextlib
import json
from typing import cast, get_type_hints

from crawlutron_extract import extract as _package_extract
from lxml import etree
from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.json_value import JsonValue
from crawlutron.models.page_metadata import PageMetadata

__all__ = [
    "PageMetadata",
    "extract_carried_metadata",
    "extract_llm_metadata",
    "llm_metadata_from_tree",
    "text_from_html",
]

#: a field added there is admitted here without a second edit; only None is stripped
#: from a union, and the system-type field resolves to the JSON union's membership.
_JSON_MEMBERSHIP = (type(None), bool, int, float, str, list, dict)
_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {}
for _field, _hint in get_type_hints(PageMetadata).items():
    if _hint is JsonValue:
        _FIELD_TYPES[_field] = _JSON_MEMBERSHIP
        continue
    _members = getattr(_hint, "__args__", ()) or ()
    if not _members:
        _FIELD_TYPES[_field] = _hint
        continue
    _live = tuple(arg for arg in _members if arg is not type(None))
    _FIELD_TYPES[_field] = _live[0] if len(_live) == 1 else _live


def extract_carried_metadata(value: object) -> PageMetadata:
    """The one place unproven page metadata becomes :class:`PageMetadata`: the JSON
    from the engine spill is admitted field by field (a junk field drops, the rest
    survives).
    """
    if not isinstance(value, dict):
        return {}
    admitted = {
        key: item
        for key, item in value.items()
        if (expected := _FIELD_TYPES.get(key)) is not None and isinstance(item, expected)
    }
    return cast(PageMetadata, admitted)


def extract_llm_metadata(html: str, url: NormalizedUrl | None) -> PageMetadata:
    """Extract Page metadata from HTML via one crawlutron_extract pass. The package's
    metadata collector mirrors PageMetadata's key set (title, description, og/twitter
    fields, canonical, favicon, language, json_ld).
    """
    metadata: dict[str, object] = {"source_url": url.raw if url is not None else ""}
    extraction = _package_extract(
        html.encode("utf-8", "replace"), "text/html", url=url.raw if url is not None else None
    )
    metadata.update(extraction.metadata)
    raw_json_ld = metadata.get("json_ld")
    if isinstance(raw_json_ld, str):
        try:
            metadata["json_ld"] = json.loads(raw_json_ld)
        except (ValueError, TypeError):
            # The package joins multiple blocks with "\n"; parse each as a separate document.
            blocks = [block for block in raw_json_ld.split("\n") if block.strip()]
            parsed: list[object] = []
            ok = True
            for block in blocks:
                try:
                    parsed.append(json.loads(block))
                except (ValueError, TypeError):
                    ok = False
                    break
            if ok and len(blocks) > 1:
                metadata["json_ld"] = parsed
            elif ok and len(blocks) == 1:
                metadata["json_ld"] = parsed[0]
            else:
                # Malformed JSON-LD is skipped, matching the original extractor.
                metadata.pop("json_ld", None)
    return extract_carried_metadata(metadata)


def llm_metadata_from_tree(tree: etree._Element | None, url: NormalizedUrl | None) -> PageMetadata:
    """Read the page-metadata fields from an ALREADY-parsed lxml tree, shared by the
    fallback and the engine's discovery stage so the two cannot drift into different
    dialects; returns plain JSON data, the tree never escaping the caller.
    """
    if tree is None:
        return {}
    source = url.raw if url is not None else ""
    metadata: PageMetadata = {"source_url": source}
    _carry_meta_tags(metadata, tree)

    title = tree.findtext(".//title")
    if title:
        metadata["title"] = " ".join(title.split())

    parsed_json_ld: list[JsonValue] = []
    for script in tree.iter("script"):
        if (script.get("type") or "").lower() != "application/ld+json":
            continue
        block = script.text
        if not block:
            continue
        with contextlib.suppress(ValueError):
            parsed_json_ld.append(json.loads(block))
    if parsed_json_ld:
        metadata["json_ld"] = parsed_json_ld[0] if len(parsed_json_ld) == 1 else parsed_json_ld

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

    lang = tree.xpath("//html/@lang")
    if lang:
        metadata["language"] = lang[0].strip()
    elif tree.tag == "html" and (tree_lang := tree.get("lang")):
        metadata["language"] = tree_lang.strip()

    return metadata


def _carry_meta_tags(metadata: PageMetadata, tree: etree._Element) -> None:
    """Carry the ``<meta>`` tags that name a declared field into ``metadata``: one
    literal-key assignment per tag.
    """
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


def text_from_html(html: str) -> str:
    """The package's text projection: noise-stripped, whitespace-collapsed page text."""
    extraction = _package_extract(html.encode("utf-8", "replace"), "text/html", url=None)
    return extraction.text.strip()
