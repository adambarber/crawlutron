"""The body of ``POST /v1/watches``: what to revalidate, how often, and where to announce."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.watch_state import IntervalSeconds


class WatchCreateRequest(BaseModel):
    """A watch registration.

    ``url`` is text rather than a ``NormalizedUrl`` because admission is the
    handler's: constructing the value object needs this deployment's host
    policy, and a URL this deployment will not crawl has to come back as a 422
    the caller can act on -- not as a field validation error about text the
    caller sent perfectly well.
    """

    model_config = ConfigDict(extra="ignore")

    url: str = Field(..., description="Public HTTP(S) URL to revalidate.")
    interval_seconds: IntervalSeconds = Field(
        ...,
        description="Seconds between checks. The first check is one interval after registration.",
    )
    webhook_url: Annotated[
        str | None,
        Field(
            description="Where to POST a change notification. Omit for a poll-only watch.",
        ),
    ] = None
