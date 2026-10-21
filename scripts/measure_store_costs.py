"""What the job store costs, measured: the visited read-back (#140) and the commit tax (#382).

Public doors only: JobStore create/load/save and the SQLite connection, on a temp
DB in the OS scratch dir. Every probe is bounded, so the run takes about a minute.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
import tempfile
import time

from app.admission.policy import CrawlPolicy, PolicyTier
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.storage.job_store import JobStore

#: The sizes the visited cost is measured at: the growth is what matters.
VISITED_SIZES = (500, 1000, 2000, 4000)

#: The saves timed per size, and the transactions timed per synchronous mode.
PROBE_SAVES = 60
PROBE_COMMITS = 200


def _url(index: int) -> NormalizedUrl:
    return NormalizedUrl(f"https://measure.example/page/{index}", policy=HostPolicy.unrestricted())


def _store(path: str) -> JobStore:
    return JobStore(path, policy=HostPolicy.unrestricted(), tier=PolicyTier.model_defaults())


def _growth_save_durations(store: JobStore, job_id: str, start: int, count: int) -> list[float]:
    """The per-save cost of a save that adds one page, in milliseconds."""
    durations: list[float] = []
    for index in range(start, start + count):
        job = store.load(job_id)
        job.visited.add(_url(index))
        started = time.perf_counter()
        store.save(job)
        durations.append((time.perf_counter() - started) * 1e3)
    return durations


def _save_costs() -> list[tuple[int, float]]:
    """The cost of a growth save at each visited size, in milliseconds.

    Every save reads the stored set back to take the difference (#140), so the
    cost grows with the visited set: that growth is the O(n) term.
    """
    costs: list[tuple[int, float]] = []
    with tempfile.TemporaryDirectory(prefix="crawlutron-measure-") as scratch:
        store = _store(os.path.join(scratch, "measure.db"))
        job = store.create(_url(0).raw, CrawlPolicy())
        for size in VISITED_SIZES:
            while len(job.visited) < size:
                job.visited.add(_url(len(job.visited)))
            store.save(job)
            durations = _growth_save_durations(store, job.job_id, size, PROBE_SAVES)
            costs.append((size, statistics.mean(durations)))
        store.close()
    return costs


def _commit_costs() -> dict[int, float]:
    """The median per-commit cost, in milliseconds, at each synchronous mode."""
    costs: dict[int, float] = {}
    with tempfile.TemporaryDirectory(prefix="crawlutron-commit-") as scratch:
        db_path = os.path.join(scratch, "commit.db")
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (i INTEGER)")
        connection.commit()
        connection.close()
        for mode in (2, 1):  # 2 = FULL, 1 = NORMAL
            probe = sqlite3.connect(db_path)
            try:
                probe.execute(f"PRAGMA synchronous={mode}")
                durations: list[float] = []
                for index in range(PROBE_COMMITS):
                    started = time.perf_counter()
                    probe.execute("BEGIN IMMEDIATE")
                    probe.execute("INSERT INTO t VALUES (?)", (index,))
                    probe.execute("COMMIT")
                    durations.append((time.perf_counter() - started) * 1e3)
            finally:
                probe.close()
            costs[mode] = statistics.median(durations)
    return costs


def main() -> None:
    print("growth save (each reads the stored set back to take the difference):")
    for size, cost in _save_costs():
        print(f"  visited={size:5d} save={cost:.3f}ms")
    costs = _commit_costs()
    print(f"commit synchronous=FULL  median={costs[2]:.4f}ms")
    print(f"commit synchronous=NORMAL median={costs[1]:.4f}ms")
    print(f"  fsync tax per commit: {costs[2] - costs[1]:.4f}ms")


if __name__ == "__main__":
    main()
