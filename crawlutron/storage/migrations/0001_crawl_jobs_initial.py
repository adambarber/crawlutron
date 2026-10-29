"""Create the crawl job table."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import CRAWL_JOBS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(CRAWL_JOBS_SCHEMA))
