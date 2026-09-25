"""Create the completed crawl result table."""

from app.crawl.migrations.manifest import RESULTS_SCHEMA, create_table_sql
from app.crawl.migrations.runner import migration_step as step

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(RESULTS_SCHEMA),
        'DROP TABLE "results"',
    )
]
