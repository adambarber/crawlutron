"""Create the watches table and its due-sweep index."""

from app.storage.migrations.manifest import WATCHES_SCHEMA, create_index_sql, create_table_sql
from app.storage.migrations.runner import migration_step as step

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(WATCHES_SCHEMA),
        'DROP TABLE "watches"',
    ),
    *(
        step(
            create_index_sql(WATCHES_SCHEMA, index),
            f'DROP INDEX "{index.name}"',
        )
        for index in WATCHES_SCHEMA.indexes
    ),
]
