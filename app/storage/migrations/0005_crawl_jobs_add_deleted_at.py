"""Add the deleted-at tombstone column to crawl jobs so DELETE is final.
A late runner save no-ops on a tombstoned row, and every read hides one.
"""

from app.storage.migrations.ddl import add_column_step

__depends__ = {"0004_crawl_jobs_add_owner"}

steps = add_column_step("crawl_jobs", "deleted_at REAL")
