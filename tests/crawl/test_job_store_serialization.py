"""LOW fix: JobStore must survive non-JSON-serializable results and must
not silently swallow deserialization errors.

Before the fix, save() raised TypeError on datetime/bytes in results (job
state lost), and load() returned None for ANY exception, hiding corruption.
"""
import datetime
import logging

from app.crawl.job_store import JobStore


def _store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def test_save_survives_non_serializable_results(tmp_path):
    store = _store(tmp_path)
    job = store.create("http://example.com/", {"strategies": ["HTTP_DECLARED_CRAWLER"]})
    # results contain non-JSON-native types (datetime, bytes, set)
    job.results = [
        {"ts": datetime.datetime(2020, 1, 1, 12, 0, 0), "raw": b"\x00\x01binary", "tags": {1, 2, 3}},
    ]
    # Must not raise TypeError.
    store.save(job)
    loaded = store.load(job.job_id)
    assert loaded is not None
    # Values are safely stringified, not lost.
    assert loaded.results[0]["ts"] == "2020-01-01T12:00:00"
    assert isinstance(loaded.results[0]["tags"], list)


def test_load_missing_returns_none_no_warning(tmp_path, caplog):
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert store.load("does-not-exist") is None
    assert not any("job_store_load_corrupt" in r.message for r in caplog.records)


def test_load_logs_warning_on_corruption(tmp_path, caplog):
    store = _store(tmp_path)
    # Insert a row whose results column is not valid JSON.
    store._conn.execute(
        """
        INSERT INTO crawl_jobs
        (job_id, root_url, policy, status, frontier, visited, results,
         created_at, updated_at, error, stats)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("corrupt-id", "http://example.com/", "{}", "pending", "[]", "[]",
         "{not valid json", 0.0, 0.0, None, "{}"),
    )
    store._conn.commit()
    with caplog.at_level(logging.WARNING):
        assert store.load("corrupt-id") is None
    assert any("job_store_load_corrupt" in r.message for r in caplog.records)
