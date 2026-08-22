"""MEDIUM fix: JobStore must survive concurrent writes from FastAPI's
worker threads. Without a lock / busy_timeout, concurrent sqlite3 writes raise
``database is locked`` (OperationalError) or corrupt reads.
"""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from app.crawl.job_store import JobStore, JobStatus


def _store(tmp_path=None):
    # Use in-memory SQLite to avoid disk I/O and lock contention (speed).
    return JobStore(":memory:")


def test_jobstore_has_concurrency_guard():
    store = _store()
    # A lock must guard all connection use.
    assert hasattr(store, "_lock")
    lock = store._lock
    assert hasattr(lock, "acquire") and hasattr(lock, "release")
    # It must actually be usable as a mutex.
    assert lock.acquire()
    lock.release()
    # SQLite must wait for the write lock rather than failing immediately.
    timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout and timeout > 0


def test_jobstore_concurrent_writes_do_not_raise_locked():
    store = _store()
    errors = []

    def worker(i):
        try:
            job = store.create(f"http://example.com/{i}", {})
            job.status = JobStatus.RUNNING
            store.save(job)
            store.load(job.job_id)
            store.delete(job.job_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(worker, range(200)))

    locked = [e for e in errors if isinstance(e, sqlite3.OperationalError)]
    assert not locked, f"concurrent writes raised OperationalError: {locked[:3]}"
    assert not errors, f"unexpected errors: {errors[:3]}"
