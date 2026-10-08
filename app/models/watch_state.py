"""The durable state of one registered watch, as a row and on the wire."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.models.content_validators import ContentValidators
from app.models.watch_outcome import WatchOutcome
from app.models.watch_status import WatchStatus

IntervalSeconds = Annotated[
    int,
    Field(gt=0, description="Seconds between revalidation checks of this watch."),
]


class WatchState(BaseModel):
    """One row of the watches table, in the shape the store and the API share.

    ``url`` is the durable text identity (``NormalizedUrl.raw`` at the
    admission edge): a watch outlives the request that created it, so the
    row stores text and every consumer re-admits it against the live host
    policy rather than trusting a decision a past deployment made.
    """

    watch_id: str
    url: str
    webhook_url: Annotated[
        str | None,
        Field(description="Change-notification target; absent means a poll-only watch."),
    ] = None
    interval_seconds: IntervalSeconds
    status: WatchStatus = WatchStatus.ACTIVE
    validators: ContentValidators = Field(default_factory=ContentValidators)
    next_check_at: float = Field(description="Epoch time of the next scheduled check.")
    last_checked_at: float | None = None
    last_outcome: WatchOutcome | None = None
    last_changed_at: float | None = None
    consecutive_failures: Annotated[int, Field(ge=0)] = 0
    created_at: float
    updated_at: float
