"""A link found on a crawled page, and what may be queued from it."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.normalized_url import NormalizedUrl


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    # The frontier/visited-eligible form; producers build it from
    # ``normalize_public_http_url`` output (``frontier_link_url`` is the only
    # read allowed for queueing -- the raw ``url`` href stays unqueue-able).
    normalized_url: NormalizedUrl
    text: str
    rel: list[str]
    source_url: str
    host: str
    is_same_host: bool
