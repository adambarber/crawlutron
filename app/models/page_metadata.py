"""What the metadata extractor can fill, declared as the owner's key set."""

from __future__ import annotations

from typing import TypedDict


class PageMetadata(TypedDict, total=False):
    """What the extractor can fill, owned here and projected by ``PageMetadataResponse``.

    ``total=False`` because the extractor sets a key only when the page
    actually carries it: a page with no ``og:image`` has no ``og_image``. The
    key SET is therefore fixed even though the presence of any one key is not.

    Living at this stem is load-bearing, not cosmetic. The mirror invariant in
    ``tests/invariants/test_wire_mirror.py`` pairs an owner with its projection
    by module name, so this file and ``page_metadata_response.py`` is the pair;
    a differently-named record would simply not be compared, which is how three
    wire fields survived with no producer.

    ``json_ld`` is genuinely open -- one parsed JSON-LD block is an arbitrary
    object, a list of them is an arbitrary list -- so it stays ``dict | list``
    rather than pretending to a shape the document chooses.
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
    json_ld: dict | list
