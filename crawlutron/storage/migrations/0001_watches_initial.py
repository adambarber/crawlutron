"""Create the watches table and its due-sweep index."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import WATCHES_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(WATCHES_SCHEMA))
