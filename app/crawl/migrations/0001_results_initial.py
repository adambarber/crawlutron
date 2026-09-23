"""Create the completed crawl result table."""

from yoyo import step

from app.crawl.migrations.manifest import RESULTS_SCHEMA, create_table_sql

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(RESULTS_SCHEMA),
        'DROP TABLE "results"',
    )
]
