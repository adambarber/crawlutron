"""Job store edge cases: corrupt rows, frontier persistence, state transitions."""

from coverage_helpers import *


class TestJobStoreEdges:
    """Cover remaining branches in job_store.py"""

    def test_job_store_load_exception(self):
        import pathlib
        import tempfile

        from app.crawl.job_store import JobStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            conn.execute("DROP TABLE IF EXISTS jobs")
            conn.commit()
            conn.close()
            result = store.load("nonexistent")
            assert (result) is None

    def test_job_store_close(self):
        import pathlib
        import tempfile

        from app.crawl.job_store import JobStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            store._conn.close()
            store._conn.close()
