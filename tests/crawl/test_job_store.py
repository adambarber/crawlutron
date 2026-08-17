"""#3+#14 JobStore - pure tests (no mocks)."""

import tempfile
import os

from app.crawl.job_store import JobStore, JobStatus, CrawlJobState


def test_jobstore_create_and_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = JobStore(db_path)

        job = store.create("http://example.com/", {"strategies": ["HTTP_DECLARED_CRAWLER"]})

        assert job.job_id
        assert job.root_url == "http://example.com/"
        assert job.status == JobStatus.PENDING
        assert job.frontier == ["http://example.com/"]
        assert job.visited == set()

        loaded = store.load(job.job_id)
        assert loaded is not None
        assert loaded.job_id == job.job_id
        assert loaded.root_url == job.root_url
        assert loaded.status == JobStatus.PENDING
        assert loaded.frontier == job.frontier


def test_jobstore_save_updates_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = JobStore(db_path)

        job = store.create("http://example.com/", {})
        job.status = JobStatus.RUNNING
        job.frontier = ["http://example.com/page2"]
        job.visited = {"http://example.com/"}
        job.results = [{"url": "http://example.com/", "status": "success"}]
        job.stats = {"total_discovered": 2, "total_crawled": 1, "total_failed": 0}
        store.save(job)

        loaded = store.load(job.job_id)
        assert loaded.status == JobStatus.RUNNING
        assert loaded.frontier == ["http://example.com/page2"]
        assert loaded.visited == {"http://example.com/"}
        assert len(loaded.results) == 1
        assert loaded.stats["total_crawled"] == 1


def test_jobstore_list_jobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = JobStore(db_path)

        job1 = store.create("http://site1.com/", {})
        job2 = store.create("http://site2.com/", {})
        job2.status = JobStatus.RUNNING
        store.save(job2)

        jobs = store.list_jobs()
        assert len(jobs) == 2
        # Sorted by created_at desc
        assert jobs[0]["job_id"] == job2.job_id
        assert jobs[1]["job_id"] == job1.job_id
        assert jobs[0]["status"] == "running"
        assert jobs[1]["status"] == "pending"


def test_jobstore_delete():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = JobStore(db_path)

        job = store.create("http://example.com/", {})
        assert store.delete(job.job_id) is True
        assert store.load(job.job_id) is None
        assert store.delete(job.job_id) is False  # already deleted


def test_jobstore_load_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = JobStore(db_path)

        assert store.load("nonexistent") is None


def test_crawljobstate_serialization():
    job = CrawlJobState(
        job_id="test123",
        root_url="http://example.com/",
        policy={"strategies": ["HTTP_DECLARED_CRAWLER"]},
        status=JobStatus.RUNNING,
        frontier=["http://example.com/a", "http://example.com/b"],
        visited={"http://example.com/"},
        results=[{"url": "http://example.com/", "status": "success"}],
        stats={"total_discovered": 3, "total_crawled": 1, "total_failed": 0},
    )

    json_data = job.to_json()
    assert json_data["job_id"] == "test123"
    assert json_data["status"] == "running"
    assert set(json_data["visited"]) == {"http://example.com/"}

    loaded = CrawlJobState.from_json(json_data)
    assert loaded.job_id == "test123"
    assert loaded.status == JobStatus.RUNNING
    assert loaded.frontier == ["http://example.com/a", "http://example.com/b"]
    assert loaded.visited == {"http://example.com/"}
    assert len(loaded.results) == 1
