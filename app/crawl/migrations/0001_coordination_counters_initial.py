"""Create the cross-process counter table backing /metrics aggregation."""

from app.crawl.migrations.manifest import COORDINATION_COUNTERS_SCHEMA, create_table_sql
from app.crawl.migrations.runner import migration_step as step

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(COORDINATION_COUNTERS_SCHEMA),
        'DROP TABLE "coordination_counters"',
    )
]
