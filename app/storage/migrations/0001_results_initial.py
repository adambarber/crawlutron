"""Create the completed crawl result table."""

from app.storage.migrations.ddl import table_steps
from app.storage.migrations.manifest import RESULTS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(RESULTS_SCHEMA))
