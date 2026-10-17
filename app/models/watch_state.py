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
    """One row of the watches table, in the shape the store and API share.

    ``url`` stores the durable text identity, re-admitted by every consumer against
    live host policy -- never a decision a past deployment made.
    """

    watch_id: str
    # The caller that owns this row: "" (anonymous bucket) or a keyed caller's
    # id. Registry CRUD is owner-scoped; the sweep reads it to inherit cookies.
    owner: Annotated[
        str,
        Field(description="Owner id (empty means the anonymous bucket)."),
    ] = ""
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
