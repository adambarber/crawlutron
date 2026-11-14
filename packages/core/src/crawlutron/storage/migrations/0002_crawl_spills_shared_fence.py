"""Create the shared crawl spill lease table."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import CRAWL_SPILL_LEASES_SCHEMA

__depends__ = {"0001_crawl_spills_initial"}

steps = list(table_steps(CRAWL_SPILL_LEASES_SCHEMA))
