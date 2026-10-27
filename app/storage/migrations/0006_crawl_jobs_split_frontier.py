"""Move the frontier queue off the job row into a delta-appended table (#547).

The ``frontier`` column re-serialized the whole queue per batch persist; as a
keyed table a batch costs one INSERT per admitted entry, one DELETE per pop.
"""

import json
from typing import TYPE_CHECKING

from app.models.json_value import JsonValue
from app.storage.migrations.manifest import CRAWL_JOB_FRONTIER_SCHEMA, create_index_sql, create_table_sql
from app.storage.migrations.runner import migration_step as step
from app.storage.sqlite_store import table_columns

if TYPE_CHECKING:
    import sqlite3

__depends__ = {"0005_crawl_jobs_add_deleted_at"}

_CREATE = create_table_sql(CRAWL_JOB_FRONTIER_SCHEMA)
_DROP_TABLE = 'DROP TABLE IF EXISTS "crawl_job_frontier"'


def _json_list(raw: object) -> list[JsonValue]:
    """A column's JSON list, or nothing if never valid JSON. A legacy database
    is one this code did not write, so an unparseable value reads as no queue
    rather than refusing to start (the 0003 shape)."""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _entries(raw: object):
    """The usable (url, depth) pairs a legacy frontier column holds."""
    for entry in _json_list(raw):
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        depth = entry.get("depth", 0)
        if not isinstance(url, str) or not url:
            continue
        try:
            depth = max(0, int(str(depth)))
        except (TypeError, ValueError):
            depth = 0
        yield url, depth


def _carry_across(conn: "sqlite3.Connection") -> None:
    """Move whatever the column already holds into the new table, then drop it:
    a fresh database no-ops both copies, while an upgraded one keeps its queue
    so a crawl in flight resumes with the entries it had outstanding."""
    columns = {column.name for column in table_columns(conn, "crawl_jobs")}
    if "frontier" not in columns:
        return
    rows = conn.execute("SELECT job_id, frontier FROM crawl_jobs").fetchall()
    conn.executemany(
        'INSERT OR IGNORE INTO "crawl_job_frontier" (job_id, pos, url, depth) VALUES (?, ?, ?, ?)',
        ((job_id, pos, url, depth) for job_id, raw in rows for pos, (url, depth) in enumerate(_entries(raw))),
    )
    conn.execute('ALTER TABLE "crawl_jobs" DROP COLUMN "frontier"')


def apply_step(conn: "sqlite3.Connection") -> None:
    existing = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if CRAWL_JOB_FRONTIER_SCHEMA.name not in existing:
        conn.execute(_CREATE)
        for index in CRAWL_JOB_FRONTIER_SCHEMA.indexes:
            conn.execute(create_index_sql(CRAWL_JOB_FRONTIER_SCHEMA, index))
    _carry_across(conn)


def rollback_step(conn: "sqlite3.Connection") -> None:
    """Restore the column and carry the live queue back into it.

    The table is the queue's only home after the cutover, so the rollback can
    restore it exactly -- unlike 0003, where the dropped history was gone."""
    cursor = conn.cursor()
    rows = cursor.execute(
        'SELECT job_id, url, depth FROM "crawl_job_frontier"'
        " WHERE job_id IN (SELECT job_id FROM crawl_jobs) ORDER BY job_id, pos"
    ).fetchall()
    queues: dict[str, list[dict[str, object]]] = {}
    for job_id, url, depth in rows:
        queues.setdefault(str(job_id), []).append({"url": url, "depth": depth})
    payload = [(json.dumps(queues[job_id], separators=(",", ":")), job_id) for job_id in queues]
    cursor.execute(_DROP_TABLE)
    columns = {column.name for column in table_columns(conn, "crawl_jobs")}
    if "frontier" not in columns:
        cursor.execute("ALTER TABLE crawl_jobs ADD COLUMN frontier TEXT NOT NULL DEFAULT '[]'")
    cursor.executemany("UPDATE crawl_jobs SET frontier = ? WHERE job_id = ?", payload)


steps = [step(apply_step, rollback_step)]
