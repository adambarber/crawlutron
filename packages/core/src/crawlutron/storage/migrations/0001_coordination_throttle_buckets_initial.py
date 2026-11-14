"""Create the cross-process throttle bucket table for shared pre-auth meters."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import COORDINATION_THROTTLE_BUCKETS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(COORDINATION_THROTTLE_BUCKETS_SCHEMA))
