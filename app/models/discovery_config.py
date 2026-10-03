from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryConfig(BaseModel):
    """Typed discovery toggles for crawl policy.

    Only recognized keys are accepted; extra keys are rejected.

    Every channel defaults to off, which is what "set robots, sitemaps, or feeds
    true to enable that source" in each field's own description says. These
    defaulted to on, and nothing noticed for two reasons: a consumer that wanted
    one channel read the others with a defaulted lookup over the raw mapping, so
    an absent key and an explicit ``false`` were the same answer; and the block
    is optional, so a policy that never mentioned discovery reached the consumer
    as an empty mapping rather than as this model. Making the model
    authoritative removed both hiding places and turned ``{"discovery": {}}``
    from "no discovery" into "fetch robots, sitemaps and feeds" -- the opposite
    of what a caller asking for nothing gets. Off is also the cheaper default to
    be wrong about in the safe direction, since every channel here costs a
    request.
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
