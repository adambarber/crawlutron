"""What the metadata extractor can fill, declared as the owner's key set."""

from __future__ import annotations

from typing import TypedDict

from app.models.json_value import JsonValue


class PageMetadata(TypedDict, total=False):
    """What the extractor can fill, owned here, projected by ``PageMetadataResponse``.

        ``total=False`` (a key exists only when the page carries it); the mirror invariant
    pairs owner/projection by module name; ``json_ld`` is the system type, no shape promised.
    """

    source_url: str
    title: str
    description: str
    language: str
    og_title: str
    og_description: str
    og_image: str
    og_url: str
    og_type: str
    og_site_name: str
    twitter_card: str
    twitter_title: str
    twitter_description: str
    twitter_image: str
    canonical_url: str
    favicon: str
    json_ld: JsonValue
