"""#3+#14 Resumable whole-site crawl job persistence (SQLite-backed).

Provides a job store for managing long-running crawl jobs with pause/resume
capability. Jobs persist their frontier, visited set, and results to SQLite
so they can survive process restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.crawl.ids import new_job_id


def _json_default(obj):
    """JobStore save fallback -- now the shared app.json_safe chokepoint."""
    from app.json_safe import json_default

    return json_default(obj)


class JobKind(str, Enum):
    """Kind of crawl job."""

    WHOLE_SITE = "whole_site"  # Recursive crawl with frontier/visited
    SINGLE_SHOT = "single_shot"  # Single async crawl (no frontier/visited)


class JobStatus(str, Enum):
    """Lifecycle status of a crawl job."""

    PENDING = "pending"  # Created, not yet started
    RUNNING = "running"  # Actively crawling
    PAUSED = "paused"  # Paused by user
    COMPLETED = "completed"  # Frontier exhausted / crawl finished
    FAILED = "failed"  # Fatal error
    CANCELLED = "cancelled"  # Explicitly cancelled


@dataclass
class CrawlJobState:
    """Persistent state of a crawl job (whole-site or single-shot)."""

    job_id: str
    kind: JobKind = JobKind.WHOLE_SITE
    root_url: str = ""
    policy: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    # Whole-site fields (used when kind == WHOLE_SITE)
    frontier: list[str] = field(default_factory=list)  # URLs pending crawl
    visited: set[str] = field(default_factory=set)  # URLs already crawled
    in_flight: list[str] = field(default_factory=list)  # batch inside the engine call
    transient_retries: dict[str, int] = field(default_factory=dict)  # url -> transient retry count
    # Single-shot fields (used when kind == SINGLE_SHOT)
    # root_url holds the first URL (or "" for batch); results holds completed crawl results
    results: list[dict[str, Any]] = field(default_factory=list)  # Completed results
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "total_discovered": 0,
            "total_crawled": 0,
            "total_failed": 0,
        }
    )

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        data = asdict(self)
        data["visited"] = list(self.visited)
        data["status"] = self.status.value
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CrawlJobState:
        """Deserialize from JSON dict."""
        data = dict(data)
        data["visited"] = set(data.get("visited", []))
        data["status"] = JobStatus(data["status"])
        data["kind"] = JobKind(data.get("kind", JobKind.WHOLE_SITE.value))
        return cls(**data)


class JobStore:
    """SQLite-backed store for crawl job states."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # Wait for the write lock instead of raising "database is locked" under
        # concurrent access from FastAPI's worker threads.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Enable WAL mode for better concurrent read/write performance
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Serialize all connection use across threads (sqlite3 connections are
        # not safe for concurrent use even with check_same_thread=False).
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'whole_site',  -- JobKind enum value
                root_url TEXT NOT NULL DEFAULT '',
                policy TEXT NOT NULL DEFAULT '{}',        -- JSON
                status TEXT NOT NULL,           -- JobStatus enum value
                frontier TEXT NOT NULL DEFAULT '[]',      -- JSON array of URLs
                visited TEXT NOT NULL DEFAULT '[]',       -- JSON array of URLs
                in_flight TEXT NOT NULL DEFAULT '[]',      -- JSON array: batch inside the engine call
                transient_retries TEXT NOT NULL DEFAULT '{}', -- JSON dict: url -> transient retry count
                results TEXT NOT NULL DEFAULT '[]',       -- JSON array of result objects
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT,
                stats TEXT NOT NULL DEFAULT '{}'            -- JSON dict
            )
        """)
        # Migration: add kind column if table was created before this column existed
        try:
            self._conn.execute("ALTER TABLE crawl_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'whole_site'")
            self._conn.commit()
        except sqlite3.OperationalError:
            # Column already exists; ignore
            pass
        # Migration: add in_flight column for mid-batch crash recovery (re-queue).
        try:
            self._conn.execute("ALTER TABLE crawl_jobs ADD COLUMN in_flight TEXT NOT NULL DEFAULT '[]'")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: add transient_retries for bounded whole-site transient retries.
        try:
            self._conn.execute(
                "ALTER TABLE crawl_jobs ADD COLUMN transient_retries TEXT NOT NULL DEFAULT '{}'"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def _row_to_job(self, row) -> CrawlJobState:
        return CrawlJobState(
            job_id=row[0],
            kind=JobKind(row[1]),
            root_url=row[2],
            policy=json.loads(row[3]),
            status=JobStatus(row[4]),
            frontier=json.loads(row[5]),
            visited=set(json.loads(row[6])),
            results=json.loads(row[7]),
            created_at=row[8],
            updated_at=row[9],
            error=row[10],
            stats=json.loads(row[11]),
            in_flight=json.loads(row[12]),
            transient_retries=json.loads(row[13]),
        )

    def create(self, root_url: str, policy: dict[str, Any]) -> CrawlJobState:
        """Create a new whole-site crawl job with initial frontier containing root_url."""
        # Full UUID hex (32 chars) to avoid brute-forceable short IDs (HIGH #9).
        job_id = new_job_id()
        job = CrawlJobState(
            job_id=job_id,
            kind=JobKind.WHOLE_SITE,
            root_url=root_url,
            policy=policy,
            status=JobStatus.PENDING,
            frontier=[root_url],
        )
        with self._lock:
            self._persist(job)
        return job

    def create_single_shot(self, job_id: str, root_url: str, policy: dict[str, Any]) -> CrawlJobState:
        """Create a new single-shot async crawl job with the given job_id (request_id)."""
        job = CrawlJobState(
            job_id=job_id,
            kind=JobKind.SINGLE_SHOT,
            root_url=root_url,
            policy=policy,
            status=JobStatus.PENDING,
        )
        with self._lock:
            self._persist(job)
        return job

    def save(self, job: CrawlJobState) -> None:
        """Persist job state to SQLite (thread-safe)."""
        with self._lock:
            self._persist(job)

    def _persist(self, job: CrawlJobState) -> None:
        """Write job state to SQLite. Caller must hold ``self._lock``."""
        job.updated_at = time.time()
        self._conn.execute(
            """
                  INSERT INTO crawl_jobs (job_id, kind, root_url, policy, status, frontier, visited, results,
                                          created_at, updated_at, error, stats, in_flight, transient_retries)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                kind=excluded.kind,
                root_url=excluded.root_url,
                policy=excluded.policy,
                status=excluded.status,
                frontier=excluded.frontier,
                visited=excluded.visited,
                results=excluded.results,
                updated_at=excluded.updated_at,
                error=excluded.error,
                      stats=excluded.stats,
                      in_flight=excluded.in_flight,
                      transient_retries=excluded.transient_retries
              """,
            (
                job.job_id,
                job.kind.value,
                job.root_url,
                json.dumps(job.policy, separators=(",", ":"), default=_json_default),
                job.status.value,
                json.dumps(job.frontier, separators=(",", ":"), default=_json_default),
                json.dumps(list(job.visited), separators=(",", ":"), default=_json_default),
                json.dumps(job.results, separators=(",", ":"), default=_json_default),
                job.created_at,
                job.updated_at,
                job.error,
                json.dumps(job.stats, separators=(",", ":"), default=_json_default),
                json.dumps(job.in_flight, separators=(",", ":"), default=_json_default),
                json.dumps(job.transient_retries, separators=(",", ":"), default=_json_default),
            ),
        )
        self._conn.commit()

    def load(self, job_id: str) -> CrawlJobState | None:
        """Load job state from SQLite. Returns None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, kind, root_url, policy, status, frontier, visited, results, "
                "created_at, updated_at, error, stats, in_flight, transient_retries FROM crawl_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._row_to_job(row)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            # Surface corruption instead of silently returning None so a
            # broken row is observable in logs rather than masked.
            logger.warning("job_store_load_corrupt", extra={"job_id": job_id, "error": str(exc)})
            return None

    def delete(self, job_id: str) -> bool:
        """Delete job from SQLite. Returns True if existed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs with basic info (no frontier/visited/results)."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT job_id, root_url, status, kind, created_at, updated_at,
                       json_array_length(frontier) as frontier_size,
                       json_array_length(visited) as visited_size,
                       json_array_length(results) as results_count
                FROM crawl_jobs
                ORDER BY created_at DESC
            """).fetchall()
        return [
            {
                "job_id": r[0],
                "root_url": r[1],
                "status": r[2],
                "kind": r[3],
                "created_at": r[4],
                "updated_at": r[5],
                "frontier_size": r[6] or 0,
                "visited_size": r[7] or 0,
                "results_count": r[8] or 0,
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection (thread-safe)."""
        with self._lock:
            self._conn.close()
