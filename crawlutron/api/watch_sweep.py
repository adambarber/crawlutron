"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.watch_sweep import (
    WATCH_SWEEP_INTERVAL_SECONDS as WATCH_SWEEP_INTERVAL_SECONDS,
)
from crawlutron.service.watch_sweep import (
    WatchSweeper as WatchSweeper,
)
from crawlutron.service.watch_sweep import (
    sweep_watches_forever as sweep_watches_forever,
)
