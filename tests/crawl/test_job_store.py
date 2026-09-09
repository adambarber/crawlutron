"""#3+#14 JobStore - pure tests (no mocks).

Uses the shared ``job_store`` fixture (conftest.py) and pytest's ``tmp_path``
instead of per-test ``tempfile.TemporaryDirectory`` boilerplate.
"""

from app.crawl.job_store import CrawlJobState, JobStatus, JobStore


def test_jobstore_create_and_load(job_store):
    job = job_store.create("http://example.com/", {"strategies": ["HTTP_DECLARED_CRAWLER"]})

    assert job.job_id
    assert job.root_url == "http://example.com/"
    assert job.status == JobStatus.PENDING
    assert job.frontier == ["http://example.com/"]
    assert job.visited == set()

    loaded = job_store.load(job.job_id)
    assert loaded is not None
    assert loaded.job_id == job.job_id
    assert loaded.root_url == job.root_url
    assert loaded.status == JobStatus.PENDING
    assert loaded.frontier == job.frontier


def test_jobstore_save_updates_state(job_store):
    job = job_store.create("http://example.com/", {})
    job.status = JobStatus.RUNNING
    job.frontier = ["http://example.com/page2"]
    job.visited = {"http://example.com/"}
    job.results = [{"url": "http://example.com/", "status": "success"}]
    job.stats = {"total_discovered": 2, "total_crawled": 1, "total_failed": 0}
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    assert loaded.status == JobStatus.RUNNING
    assert loaded.frontier == ["http://example.com/page2"]
    assert loaded.visited == {"http://example.com/"}
    assert len(loaded.results) == 1
    assert loaded.stats["total_crawled"] == 1


def test_jobstore_list_jobs(job_store):
    job1 = job_store.create("http://site1.com/", {})
    job2 = job_store.create("http://site2.com/", {})
    job2.status = JobStatus.RUNNING
    job_store.save(job2)

    jobs = job_store.list_jobs()
    assert len(jobs) == 2
    # Sorted by created_at desc
    assert jobs[0]["job_id"] == job2.job_id
    assert jobs[1]["job_id"] == job1.job_id
    assert jobs[0]["status"] == "running"
    assert jobs[1]["status"] == "pending"


def test_jobstore_delete(job_store):
    job = job_store.create("http://example.com/", {})
    assert job_store.delete(job.job_id) is True
    assert job_store.load(job.job_id) is None
    assert job_store.delete(job.job_id) is False  # already deleted


def test_jobstore_load_missing_returns_none(job_store):
    assert job_store.load("nonexistent") is None


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


def test_create_returns_full_uuid_hex():
    """HIGH #9: job IDs must not be short brute-forceable suffixes."""
    store = JobStore(":memory:")
    job = store.create("https://example.com/", {"limits": {}})
    assert len(job.job_id) == 32
    assert all(c in "0123456789abcdef" for c in job.job_id)
    loaded = store.load(job.job_id)
    assert loaded is not None
    assert loaded.job_id == job.job_id


def test_ids_are_unique():
    store = JobStore(":memory:")
    ids = {store.create("https://example.com/", {"limits": {}}).job_id for _ in range(50)}
    assert len(ids) == 50
