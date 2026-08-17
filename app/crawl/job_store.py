"""#3+#14 Resumable whole-site crawl job persistence (SQLite-backed).

Provides a job store for managing long-running crawl jobs with pause/resume
capability. Jobs persist their frontier, visited set, and results to SQLite
so they can survive process restarts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    """Lifecycle status of a crawl job."""
    PENDING = "pending"       # Created, not yet started
    RUNNING = "running"       # Actively crawling
    PAUSED = "paused"         # Paused by user
    COMPLETED = "completed"   # Frontier exhausted
    FAILED = "failed"         # Fatal error
    CANCELLED = "cancelled"   # Explicitly cancelled


@dataclass
class CrawlJobState:
    """Persistent state of a whole-site crawl job."""
    job_id: str
    root_url: str
    policy: dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    frontier: list[str] = field(default_factory=list)   # URLs pending crawl
    visited: set[str] = field(default_factory=set)      # URLs already crawled
    results: list[dict[str, Any]] = field(default_factory=list)  # Completed results
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    stats: dict[str, int] = field(default_factory=lambda: {
        "total_discovered": 0,
        "total_crawled": 0,
        "total_failed": 0,
    })

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        data = asdict(self)
        data["visited"] = list(self.visited)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CrawlJobState":
        """Deserialize from JSON dict."""
        data = dict(data)
        data["visited"] = set(data.get("visited", []))
        data["status"] = JobStatus(data["status"])
        return cls(**data)


class JobStore:
    """SQLite-backed store for crawl job states."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                job_id TEXT PRIMARY KEY,
                root_url TEXT NOT NULL,
                policy TEXT NOT NULL,           -- JSON
                status TEXT NOT NULL,           -- JobStatus enum value
                frontier TEXT NOT NULL,         -- JSON array of URLs
                visited TEXT NOT NULL,          -- JSON array of URLs
                results TEXT NOT NULL,          -- JSON array of result objects
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT,
                stats TEXT NOT NULL             -- JSON dict
            )
        """)
        self._conn.commit()

    def _row_to_job(self, row) -> CrawlJobState:
        return CrawlJobState(
            job_id=row[0],
            root_url=row[1],
            policy=json.loads(row[2]),
            status=JobStatus(row[3]),
            frontier=json.loads(row[4]),
            visited=set(json.loads(row[5])),
            results=json.loads(row[6]),
            created_at=row[7],
            updated_at=row[8],
            error=row[9],
            stats=json.loads(row[10]),
        )

    def create(self, root_url: str, policy: dict[str, Any]) -> CrawlJobState:
        """Create a new crawl job with initial frontier containing root_url."""
        job_id = uuid.uuid4().hex[:12]
        job = CrawlJobState(
            job_id=job_id,
            root_url=root_url,
            policy=policy,
            status=JobStatus.PENDING,
            frontier=[root_url],
        )
        self.save(job)
        return job

    def save(self, job: CrawlJobState) -> None:
        """Persist job state to SQLite."""
        job.updated_at = time.time()
        self._conn.execute("""
            INSERT INTO crawl_jobs (job_id, root_url, policy, status, frontier, visited, results,
                                    created_at, updated_at, error, stats)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                root_url=excluded.root_url,
                policy=excluded.policy,
                status=excluded.status,
                frontier=excluded.frontier,
                visited=excluded.visited,
                results=excluded.results,
                updated_at=excluded.updated_at,
                error=excluded.error,
                stats=excluded.stats
        """, (
            job.job_id,
            job.root_url,
            json.dumps(job.policy, separators=(",", ":")),
            job.status.value,
            json.dumps(job.frontier, separators=(",", ":")),
            json.dumps(list(job.visited), separators=(",", ":")),
            json.dumps(job.results, separators=(",", ":")),
            job.created_at,
            job.updated_at,
            job.error,
            json.dumps(job.stats, separators=(",", ":")),
        ))
        self._conn.commit()

    def load(self, job_id: str) -> CrawlJobState | None:
        """Load job state from SQLite. Returns None if not found."""
        row = self._conn.execute(
            "SELECT job_id, root_url, policy, status, frontier, visited, results, "
            "created_at, updated_at, error, stats FROM crawl_jobs WHERE job_id = ?",
            (job_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return self._row_to_job(row)
        except Exception:
            return None

    def delete(self, job_id: str) -> bool:
        """Delete job from SQLite. Returns True if existed."""
        cur = self._conn.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs with basic info (no frontier/visited/results)."""
        rows = self._conn.execute("""
            SELECT job_id, root_url, status, created_at, updated_at,
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
                "created_at": r[3],
                "updated_at": r[4],
                "frontier_size": r[5] or 0,
                "visited_size": r[6] or 0,
                "results_count": r[7] or 0,
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
