"""Create the crawl job table."""

from app.storage.migrations.manifest import CRAWL_JOBS_SCHEMA, create_table_sql
from app.storage.migrations.runner import migration_step as step

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(CRAWL_JOBS_SCHEMA),
        'DROP TABLE "crawl_jobs"',
    )
]
