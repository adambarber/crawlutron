from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_has_column

__depends__ = {"0001_results_initial"}


def apply_step(conn) -> None:
    # owner="" is the shared anonymous bucket; historical rows backfill into
    # it so they stay readable anonymously (compatibility contract).
    if not table_has_column(conn, "results", "owner"):
        conn.execute('ALTER TABLE "results" ADD COLUMN "owner" TEXT NOT NULL DEFAULT \'\'')


def rollback_step(conn) -> None:
    if table_has_column(conn, "results", "owner"):
        conn.execute('ALTER TABLE "results" DROP COLUMN "owner"')


steps = [step(apply_step, rollback_step)]
