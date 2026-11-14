"""Create the sync-slot coordination table for cross-worker quota sharing (#942)."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import COORDINATION_SYNC_SLOTS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(COORDINATION_SYNC_SLOTS_SCHEMA))
