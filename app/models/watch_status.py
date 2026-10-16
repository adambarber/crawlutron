"""The lifecycle state of a registered watch."""

from __future__ import annotations

from enum import StrEnum


class WatchStatus(StrEnum):
    """What the due sweep asks a row's lifecycle column.

    One member today -- active vs deleted -- but the sweep's one query selects on it,
    so the column (and a documented future ``paused``) earns its place.
    """

    ACTIVE = "active"
