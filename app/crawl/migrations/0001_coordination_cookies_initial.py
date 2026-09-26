"""Create the cross-process cookie table backing shared jar storage."""

from app.crawl.migrations.manifest import COORDINATION_COOKIES_SCHEMA, create_table_sql
from app.crawl.migrations.runner import migration_step as step

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(COORDINATION_COOKIES_SCHEMA),
        'DROP TABLE "coordination_cookies"',
    )
]
