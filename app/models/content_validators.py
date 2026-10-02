from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


class ContentValidators(BaseModel):
    """Validators returned by the origin for the delivered representation.

    Feed ``etag`` / ``last_modified`` back as ``if_none_match`` /
    ``if_modified_since`` on a later request to revalidate cheaply.
    """

    etag: Annotated[
        str | None,
        Field(
            description="Origin ETag for the final representation, if provided.",
        ),
    ] = None
    last_modified: Annotated[str | None, Field(description="Origin Last-Modified date, if provided.")] = None
