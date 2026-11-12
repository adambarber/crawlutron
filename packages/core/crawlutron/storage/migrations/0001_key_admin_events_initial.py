"""Create the admin audit trail table."""

from crawlutron.storage.migrations.ddl import table_steps
from crawlutron.storage.migrations.manifest import KEY_ADMIN_EVENTS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(KEY_ADMIN_EVENTS_SCHEMA))
