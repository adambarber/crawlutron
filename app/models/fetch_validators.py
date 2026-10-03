from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class FetchValidators(BaseModel):
    """Caller-supplied cached validators for cheap conditional revalidation.

    Supply the ETag / Last-Modified you received from a previous crawl of the
    same URL; matching URLs are revalidated with If-None-Match /
    If-Modified-Since instead of fully re-fetched. A 304 yields a
    ``not_modified`` result (no content) so freshness monitors avoid paying
    full-fetch cost on unchanged pages.
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
