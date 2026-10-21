"""Create the cross-process counter table backing /metrics aggregation."""

from app.storage.migrations.ddl import table_steps
from app.storage.migrations.manifest import COORDINATION_COUNTERS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(COORDINATION_COUNTERS_SCHEMA))
