from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class FetchValidators(BaseModel):
    """Caller-supplied cached validators for cheap conditional revalidation.

    An ETag/Last-Modified from a prior crawl makes matching URLs revalidate with
    If-None-Match/If-Modified-Since; a 304 yields a ``not_modified`` (no content).
    """

    model_config = ConfigDict(extra="forbid")

    if_none_match: Annotated[
        str | None,
        Field(
            description="ETag from a previous crawl result; sent as If-None-Match.",
        ),
    ] = None
    if_modified_since: Annotated[
        str | None,
        Field(
            description="Last-Modified date from a previous crawl result; sent as If-Modified-Since.",
        ),
    ] = None
