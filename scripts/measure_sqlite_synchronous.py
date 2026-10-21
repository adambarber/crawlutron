"""#382: quantify SQLite commit frequency and WAL fsync cost on a realistic crawl.

Public doors only; a temp DB in the OS scratch dir, repo tree untouched.
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

N_PAGES = 12_000
N_SAVES = N_PAGES  # a save-per-page crawl
N_COMMIT_PROBES = 200


def _url(i: int) -> NormalizedUrl:
    return NormalizedUrl(f"https://measure.example/page/{i}", policy=HostPolicy.unrestricted())


def _growth_durations(store: JobStore, job_id: str) -> list[float]:
    """Save-per-page with visited growing to N_PAGES; returns per-save wall times."""
    durations: list[float] = []
    for i in range(1, N_SAVES + 1):
        job = store.load(job_id)
        job.visited.add(_url(i))
        t0 = time.perf_counter()
        store.save(job)
        durations.append(time.perf_counter() - t0)
    return durations


def _commit_durations(db_path: str, job_id: str) -> tuple[int, list[float]]:
    """200 tiny transactions on a separate connection; returns (synchronous, times)."""
    durations: list[float] = []
    probe = sqlite3.connect(db_path)
    try:
        sync_mode = int(probe.execute("PRAGMA synchronous").fetchone()[0])
        for _ in range(200):
            t0 = time.perf_counter()
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("UPDATE crawl_jobs SET updated_at = updated_at WHERE job_id = ?", (job_id,))
            probe.execute("COMMIT")
            durations.append(time.perf_counter() - t0)
    finally:
        probe.close()
    return sync_mode, durations


def _p(durations: list[float], q: int) -> float:
    return statistics.quantiles(durations, n=20)[q] * 1e3


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="crawlutron-sync-") as scratch:
        db_path = os.path.join(scratch, "measure.db")
        store = JobStore(db_path, policy=HostPolicy.unrestricted(), tier=PolicyTier.model_defaults())
        job = store.create(_url(0).raw, CrawlPolicy())
        durations = _growth_durations(store, job.job_id)
        sync_mode, commit_durations = _commit_durations(db_path, job.job_id)

        print(f"pages={N_PAGES} saves={N_SAVES}")
        print(f"save wall total: {sum(durations):.3f}s  mean={statistics.mean(durations) * 1e3:.3f}ms")
        print(
            f"save p50/p95/max: {_p(durations, 9):.3f}/{_p(durations, 18):.3f}/{max(durations) * 1e3:.3f} ms"
        )
        print(
            f"synchronous={sync_mode} (3=FULL default)  "
            f"mean commit={statistics.mean(commit_durations) * 1e3:.3f}ms  "
            f"p95={_p(commit_durations, 18):.3f}ms"
        )
        savings = statistics.mean(commit_durations) * N_SAVES * (1 - 1 / 3)
        print(f"FULL->NORMAL saves ~{savings:.2f}s per {N_SAVES}-save crawl (worst case)")
        store.close()


if __name__ == "__main__":
    main()
