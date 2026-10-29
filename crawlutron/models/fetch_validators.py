from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from crawlutron.safety.header_values import reject_injectable_header_value


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

    @field_validator("if_none_match", "if_modified_since")
    @classmethod
    def _reject_injectable_validator_values(cls, value: str | None, info: ValidationInfo) -> str | None:
        """A cached validator is a header VALUE at one remove: it rides
        If-None-Match/If-Modified-Since verbatim, so the wire law applies
        at the model edge.
        """
        if value is not None and info.field_name is not None:
            reject_injectable_header_value(info.field_name, value)
        return value
