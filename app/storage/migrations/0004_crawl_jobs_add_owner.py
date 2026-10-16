from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_has_column

__depends__ = {"0003_crawl_jobs_split_accumulators"}


def apply_step(conn) -> None:
    if not table_has_column(conn, "crawl_jobs", "owner"):
        conn.execute('ALTER TABLE "crawl_jobs" ADD COLUMN "owner" TEXT NOT NULL DEFAULT \'\'')


def rollback_step(conn) -> None:
    if table_has_column(conn, "crawl_jobs", "owner"):
        conn.execute('ALTER TABLE "crawl_jobs" DROP COLUMN "owner"')


steps = [step(apply_step, rollback_step)]
