"""The watch-model surface one owner: the watch registry and the sweeper tests.
The durable watch domain is imported by the store and its tests -- one owner of
the run so neither re-spells the ``app.models`` block.
"""

from __future__ import annotations

from app.models.normalized_url import NormalizedUrl
from app.models.watch_outcome import WatchOutcome
from app.models.watch_state import WatchState
from app.models.watch_status import WatchStatus

__all__ = ["NormalizedUrl", "WatchOutcome", "WatchState", "WatchStatus"]
