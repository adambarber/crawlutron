"""Inbound caller keys: issuance, verification, listing, and revocation."""

from __future__ import annotations

from app.storage.migrations.manifest import CALLER_KEYS_SCHEMA, create_index_sql, create_table_sql
from app.storage.migrations.runner import migration_step as step

__depends__: set[str] = set()

steps = [
    step(
        create_table_sql(CALLER_KEYS_SCHEMA),
        'DROP TABLE "caller_keys"',
    ),
    *(
        step(
            create_index_sql(CALLER_KEYS_SCHEMA, index),
            f'DROP INDEX "{index.name}"',
        )
        for index in CALLER_KEYS_SCHEMA.indexes
    ),
]
