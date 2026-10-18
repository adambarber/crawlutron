"""The shared test-import surface for the watch domain.
Re-exports the app's watch-model owner so a test imports from its surface (the
#6241 design) instead of re-spelling the ``app.models`` watch block.
"""

from __future__ import annotations

from app.storage.watch_surface import NormalizedUrl, WatchOutcome, WatchState, WatchStatus

__all__ = ["NormalizedUrl", "WatchOutcome", "WatchState", "WatchStatus"]
