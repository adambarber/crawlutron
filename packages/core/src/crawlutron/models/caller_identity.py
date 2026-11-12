from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from crawlutron.admission.policy import DEFAULT_MAX_PAGES

# Anonymous is a first-class bounded identity (capability defaults).
DEFAULT_ANONYMOUS_MAX_PAGES = DEFAULT_MAX_PAGES
DEFAULT_ANONYMOUS_MAX_CONCURRENCY = 4


class CallerIdentity(BaseModel):
    """Who a request crawls as: a verified key or the anonymous caller.

    Quotas ride on the identity so admission checks one value, not two lookups.
    """

    model_config = ConfigDict(extra="ignore")

    caller_id: str = ""
    authenticated: bool = False
    max_pages: int = DEFAULT_ANONYMOUS_MAX_PAGES
    max_concurrency: int = DEFAULT_ANONYMOUS_MAX_CONCURRENCY
