"""The outcome of a watch's most recent revalidation check."""

from __future__ import annotations

from enum import StrEnum


class WatchOutcome(StrEnum):
    """What the last check of a watch found.

    ``not_modified`` is the cheap 304 revalidation the validators exist
    for; ``modified`` is the state webhook delivery is pinned to;
    ``error`` is a failed check (fetch or classification), which says
    nothing about whether the page changed.
    """

    MODIFIED = "modified"
    NOT_MODIFIED = "not_modified"
    ERROR = "error"
