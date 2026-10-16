"""The outcome of a watch's most recent revalidation check."""

from __future__ import annotations

from enum import StrEnum


class WatchOutcome(StrEnum):
    """What the last check of a watch found.

    ``not_modified`` is the cheap 304 revalidation; ``modified`` is the state webhook
    delivery pins to; ``error`` is a failed check and says nothing about change.
    """

    MODIFIED = "modified"
    NOT_MODIFIED = "not_modified"
    ERROR = "error"
