"""Move the two growing collections off the job row into append-only tables:
``results``/``visited`` re-serialized the whole page history per batch, so their
monotonic appends now cost one INSERT; the mutable ``frontier`` stays a column.
"""

import json

from app.models.json_value import JsonValue
from app.storage.migrations.manifest import (
    CRAWL_JOB_RESULTS_SCHEMA,
    CRAWL_JOB_VISITED_SCHEMA,
    create_table_sql,
)
from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_columns

__depends__ = {"0001_crawl_jobs_initial"}

_CREATE = (
    (create_table_sql(CRAWL_JOB_RESULTS_SCHEMA), CRAWL_JOB_RESULTS_SCHEMA),
    (create_table_sql(CRAWL_JOB_VISITED_SCHEMA), CRAWL_JOB_VISITED_SCHEMA),
)
_DROP = (
    'DROP TABLE IF EXISTS "crawl_job_results"',
    'DROP TABLE IF EXISTS "crawl_job_visited"',
)


def _json_list(raw) -> list[JsonValue]:
    """A column's JSON list, or nothing if it was never valid JSON. A legacy database
    is one this code did not write, so an unparseable value is a real possibility:
    reading the job as having crawled nothing beats refusing to start.
    """
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _create_missing(conn) -> None:
    """Create the companion tables, tolerating ones already there: the manifest DDL
    has no IF NOT EXISTS, so a hand-pre-created table must not fail the upgrade on
    a step with nothing left to do -- or fail the schema check that follows.
    """
    existing = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    for statement, schema in _CREATE:
        if schema.name not in existing:
            conn.execute(statement)


def _carry_across(conn) -> None:
    """Move whatever the columns already hold into the new tables, then drop them: a
    fresh database no-ops both copies, while an upgraded one keeps its history so
    a crawl in flight resumes with the pages it had already committed.
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
        # The page history only exists in the tables this migration dropped, so a rollback
        # cannot restore it: a job whose results are gone must look that way.
    cursor.execute("UPDATE crawl_jobs SET results = '[]', visited = '[]'")


steps = [step(apply_step, rollback_step)]
