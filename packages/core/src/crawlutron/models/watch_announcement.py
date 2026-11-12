"""What a watch's change notification carries, as a declared record."""

from __future__ import annotations

from typing import TypedDict

from crawlutron.vocabulary.taxonomy import CrawlClassification


class WatchAnnouncement(TypedDict):
    """The body delivered to a watch's webhook when its representation changed.

    A declared key set because it is an external contract: a subscriber reads these
    five fields, and no page body travels here -- the promise is a notification.
    """

    watch_id: str
    url: str
    interval_seconds: int
    outcome: CrawlClassification
    checked_at: float


__all__ = ["WatchAnnouncement"]
