"""A link found on a crawled page, and what may be queued from it."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.normalized_url import NormalizedUrl


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    # The frontier/visited-eligible form of this link. Built by
    # ``canonical_page_identity`` at extraction, so the value IS admitted.
    normalized_url: NormalizedUrl
    text: str
    rel: list[str]
    source_url: NormalizedUrl
    host: str
    is_same_host: bool
