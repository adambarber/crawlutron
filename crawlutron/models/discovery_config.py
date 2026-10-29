from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryConfig(BaseModel):
    """Typed discovery toggles for crawl policy; only recognized keys are accepted.

    Every channel defaults to off -- the safe, cheap-to-be-wrong default, since each
    channel costs a request; ``{"discovery": {}}`` is "fetch nothing", not "everything".
    """

    robots: Annotated[
        bool,
        Field(
            description="Honor /robots.txt rules and report robots crawl-delay, sitemaps, and disallow "
            "patterns.",
        ),
    ] = False
    sitemaps: Annotated[
        bool,
        Field(
            description="Fetch and parse sitemap.xml / sitemap_index.xml URLs from robots.txt and common "
            "locations.",
        ),
    ] = False
    feeds: Annotated[
        bool,
        Field(
            description='Detect RSS/Atom/JSON Feed links via <link rel="alternate"> and common feed paths.',
        ),
    ] = False

    model_config = ConfigDict(extra="forbid")
