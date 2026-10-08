"""The lifecycle state of a registered watch."""

from __future__ import annotations

from enum import StrEnum


class WatchStatus(StrEnum):
    """What the due sweep asks a row's lifecycle column.

    One member today: a watch is registered and being checked, or it has
    been deleted outright. The column still earns its place because the
    sweep's one query selects on it -- ``paused`` is the documented next
    member, and adding it will not move the sweep's query shape.
    """

    ACTIVE = "active"
