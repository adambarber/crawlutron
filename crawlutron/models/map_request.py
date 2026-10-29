from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from crawlutron.models.crawl_policy import CrawlPolicy


class MapRequest(BaseModel):
    """Site-map request: discover the URL frontier for a root URL.

    Discovery (robots sitemaps + feeds + in-page links) is forced on; the caller
    controls the fetch ``policy`` and the rest is delegated to the shared crawl engine.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="Root public HTTP(S) URL to map.")
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Optional crawl policy for the mapping crawl (strategies, limits, scope_regex). "
        "Discovery "
        "is always enabled.",
    )
