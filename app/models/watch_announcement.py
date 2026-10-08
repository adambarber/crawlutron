"""What a watch's change notification carries, as a declared record."""

from __future__ import annotations

from typing import TypedDict

from app.models.watch_outcome import WatchOutcome


class WatchAnnouncement(TypedDict):
    """The body delivered to a watch's webhook when its representation changed.

    A declared key set because it is a contract with something outside this
    service: a subscriber reads these five fields, so a producer that quietly
    stopped selecting one has to fail at the consumer rather than arrive as a
    notification with a hole in it. No page body travels here -- the watch
    promised a notification, not a copy of the page.
    """

    watch_id: str
    url: str
    interval_seconds: int
    outcome: WatchOutcome
    checked_at: float


__all__ = ["WatchAnnouncement"]
