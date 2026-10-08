from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_columns

__depends__ = {"0001_crawl_jobs_initial"}


def apply_step(conn) -> None:
    cursor = conn.cursor()
    columns = {column.name for column in table_columns(conn, "crawl_jobs")}
    if "canonicalized" in columns:
        cursor.execute('ALTER TABLE "crawl_jobs" DROP COLUMN "canonicalized"')


def rollback_step(conn) -> None:
    cursor = conn.cursor()
    columns = {column.name for column in table_columns(conn, "crawl_jobs")}
    if "canonicalized" not in columns:
        cursor.execute('ALTER TABLE "crawl_jobs" ADD COLUMN "canonicalized" INTEGER NOT NULL DEFAULT 0')


steps = [step(apply_step, rollback_step)]
