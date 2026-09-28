"""Move the two growing collections off the job row into append-only tables.

`crawl_jobs.results` and `crawl_jobs.visited` held the job's entire history as
JSON on the row itself, so every batch persist re-serialized the whole page
history: a 3000-page job rewrote ~6 MB per batch, and total write volume over a
crawl grew as the square of the page count. Both collections are monotonic --
pages are appended and never rewritten -- so they belong in tables where a
batch costs one INSERT per new page.

`frontier` deliberately stays a column. It is a mutable queue, not an
accumulator: each batch pops entries and pushes newly discovered ones, so a
table would buy a rewrite-avoidance it cannot deliver without re-implementing
queue order on disk. At 3000 pages the frontier is ~300 KB against ~6 MB of
results, so it is not the cost that justified this change.
"""

import json

from app.crawl.migrations.manifest import (
    CRAWL_JOB_RESULTS_SCHEMA,
    CRAWL_JOB_VISITED_SCHEMA,
    create_table_sql,
)
from app.crawl.migrations.runner import migration_step as step
from app.crawl.sqlite_store import table_columns

__depends__ = {"0001_crawl_jobs_initial"}

_CREATE = (
    (create_table_sql(CRAWL_JOB_RESULTS_SCHEMA), CRAWL_JOB_RESULTS_SCHEMA),
    (create_table_sql(CRAWL_JOB_VISITED_SCHEMA), CRAWL_JOB_VISITED_SCHEMA),
)
_DROP = (
    'DROP TABLE IF EXISTS "crawl_job_results"',
    'DROP TABLE IF EXISTS "crawl_job_visited"',
)


def _json_list(raw) -> list:
    """A column's JSON list, or nothing if it was never valid JSON.

    A legacy database is by definition one this code did not write, so a value
    that will not parse is a real possibility rather than a programming error;
    reading such a job as having crawled nothing is better than refusing to
    start, and the columns are dropped either way.
    """
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _create_missing(conn) -> None:
    """Create the companion tables, tolerating ones that are already there.

    The manifest's DDL has no IF NOT EXISTS, so an operator who pre-created the
    tables by hand would fail the upgrade on a step that has nothing left to do
    -- and the schema check that follows would then reject a database that is
    in fact exactly the shape this migration produces.
    """
    existing = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    for statement, schema in _CREATE:
        if schema.name not in existing:
            conn.execute(statement)


def _carry_across(conn) -> None:
    """Move whatever the columns already hold into the new tables, then drop them.

    A fresh database has no rows and no columns to drop, so both copies no-op.
    An upgraded one keeps its history, which is the whole point of a migration:
    a crawl in flight across the deploy resumes with the pages it had already
    committed rather than silently forgetting them.
    """
    columns = {column.name for column in table_columns(conn, "crawl_jobs")}

    if "results" in columns:
        rows = conn.execute("SELECT job_id, results FROM crawl_jobs").fetchall()
        conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
            (
                (job_id, seq, json.dumps(payload, separators=(",", ":"), default=str))
                for job_id, raw in rows
                for seq, payload in enumerate(_json_list(raw))
            ),
        )
        conn.execute('ALTER TABLE "crawl_jobs" DROP COLUMN "results"')

    if "visited" in columns:
        rows = conn.execute("SELECT job_id, visited FROM crawl_jobs").fetchall()
        conn.executemany(
            'INSERT OR IGNORE INTO "crawl_job_visited" (job_id, url) VALUES (?, ?)',
            (
                (job_id, url)
                for job_id, raw in rows
                for url in _json_list(raw)
                if isinstance(url, str) and url
            ),
        )
        conn.execute('ALTER TABLE "crawl_jobs" DROP COLUMN "visited"')


def apply_step(conn) -> None:
    _create_missing(conn)
    _carry_across(conn)


def rollback_step(conn) -> None:
    cursor = conn.cursor()
    for statement in _DROP:
        cursor.execute(statement)
    columns = {column.name for column in table_columns(conn, "crawl_jobs")}
    if "results" not in columns:
        cursor.execute("ALTER TABLE crawl_jobs ADD COLUMN results TEXT NOT NULL DEFAULT '[]'")
    if "visited" not in columns:
        cursor.execute("ALTER TABLE crawl_jobs ADD COLUMN visited TEXT NOT NULL DEFAULT '[]'")
    # The page history only exists in the tables this migration dropped, so a
    # rollback cannot restore it. That is why the columns come back empty
    # rather than pretending: a rollback is a repair action, and a job whose
    # results are gone must look that way instead of looking complete.
    cursor.execute("UPDATE crawl_jobs SET results = '[]', visited = '[]'")


steps = [step(apply_step, rollback_step)]
