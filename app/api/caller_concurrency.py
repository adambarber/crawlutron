"""Per-caller in-flight register for the sync door (#194): sync crawls create no
job row to count concurrency with, so this in-memory per-caller counter is summed
with the async job-row count so one limit binds both doors."""

from __future__ import annotations

from collections import defaultdict


class CallerConcurrencyTracker:
    """Per-caller count of in-flight sync crawls, released to zero on completion."""

    def __init__(self) -> None:
        self._in_flight: dict[str, int] = defaultdict(int)

    def reserve(self, caller_id: str) -> None:
        """Mark one more sync crawl in flight for ``caller_id``."""
        self._in_flight[caller_id] += 1

    def release(self, caller_id: str) -> None:
        """Mark one sync crawl done; a caller at zero is dropped, never clamped."""
        count = self._in_flight.get(caller_id, 0) - 1
        if count <= 0:
            self._in_flight.pop(caller_id, None)
        else:
            self._in_flight[caller_id] = count

    def sync_in_flight(self, caller_id: str) -> int:
        """How many sync crawls ``caller_id`` currently has running (0 when none)."""
        return self._in_flight.get(caller_id, 0)


__all__ = ["CallerConcurrencyTracker"]
