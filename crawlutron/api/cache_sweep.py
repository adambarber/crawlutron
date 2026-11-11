"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.cache_sweep import (
    CACHE_MAINTENANCE_INTERVAL_SECONDS as CACHE_MAINTENANCE_INTERVAL_SECONDS,
)
from crawlutron.service.cache_sweep import (
    cache_maintenance_once as cache_maintenance_once,
)
from crawlutron.service.cache_sweep import (
    sweep_cache_forever as sweep_cache_forever,
)
