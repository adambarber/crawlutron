"""Ordered, reversible SQLite schema migrations."""

from app.crawl.migrations.manifest import (
    CURRENT_SCHEMAS,
    MIGRATION_IDS,
    MIGRATION_SEQUENCES,
    ColumnSchema,
    IndexSchema,
    StoreSchema,
    TableSchema,
    create_table_sql,
    schema_drift,
)
from app.crawl.migrations.runner import (
    LegacySchemaMismatch,
    migrate_store,
    rollback_store,
    stamp_store,
)

__all__ = [
    "CURRENT_SCHEMAS",
    "MIGRATION_IDS",
    "MIGRATION_SEQUENCES",
    "ColumnSchema",
    "IndexSchema",
    "LegacySchemaMismatch",
    "StoreSchema",
    "TableSchema",
    "create_table_sql",
    "migrate_store",
    "rollback_store",
    "schema_drift",
    "stamp_store",
]
