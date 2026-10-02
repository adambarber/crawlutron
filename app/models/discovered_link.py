"""A link found on a crawled page, and what may be queued from it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    # The frontier/visited-eligible form; producers build it from
    # ``normalize_public_http_url`` output (``frontier_link_url`` is the only
    # read allowed for queueing -- the raw ``url`` href stays unqueue-able).
    #
    # ``str``, not ``NormalizedUrl``, because this record crosses the wire
    # inside the crawl result and is rebuilt from the spill file. Admission is
    # not weaker for it: ``_candidate_stream`` re-proves every candidate through
    # ``canonical_page_identity`` at the queueing door, so the guarantee lives
    # there rather than in what a record remembers about itself.
    normalized_url: str
    text: str
    rel: list[str]
    source_url: str
    host: str
    is_same_host: bool
