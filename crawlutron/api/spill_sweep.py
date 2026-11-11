"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.spill_sweep import (
    SPILL_SWEEP_INTERVAL_SECONDS as SPILL_SWEEP_INTERVAL_SECONDS,
)
from crawlutron.service.spill_sweep import (
    spill_maintenance_once as spill_maintenance_once,
)
from crawlutron.service.spill_sweep import (
    sweep_spills_forever as sweep_spills_forever,
)
