"""Create the crawl job table."""

from yoyo import step

from app.crawl.migrations.manifest import CRAWL_JOBS_SCHEMA, create_table_sql

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(CRAWL_JOBS_SCHEMA),
        'DROP TABLE "crawl_jobs"',
    )
]
