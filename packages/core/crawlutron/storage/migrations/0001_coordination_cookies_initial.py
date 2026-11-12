"""Create the cross-process cookie table backing shared jar storage."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import COORDINATION_COOKIES_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(COORDINATION_COOKIES_SCHEMA))
