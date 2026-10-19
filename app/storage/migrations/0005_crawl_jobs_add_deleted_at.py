"""Add the deleted-at tombstone column to crawl jobs so DELETE is final.
A late runner save no-ops on a tombstoned row, and every read hides one.
"""

from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_has_column

__depends__ = {"0004_crawl_jobs_add_owner"}


def apply_step(conn) -> None:
    if not table_has_column(conn, "crawl_jobs", "deleted_at"):
        conn.execute('ALTER TABLE "crawl_jobs" ADD COLUMN "deleted_at" REAL')


def rollback_step(conn) -> None:
    if table_has_column(conn, "crawl_jobs", "deleted_at"):
        conn.execute('ALTER TABLE "crawl_jobs" DROP COLUMN "deleted_at"')


steps = [step(apply_step, rollback_step)]
