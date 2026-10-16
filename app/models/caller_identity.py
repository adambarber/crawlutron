from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CallerIdentity(BaseModel):
    """Who a request crawls as: a verified key or the anonymous caller.

    Quotas ride on the identity so admission checks one value, not two lookups.
    """

    model_config = ConfigDict(extra="ignore")

    caller_id: str = ""
    label: str = "anonymous"
    authenticated: bool = False
    max_pages: int = 0
    max_concurrency: int = 0
