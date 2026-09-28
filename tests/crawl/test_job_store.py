"""Job store: serialization, concurrency, purge and lifecycle edges."""

import datetime
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.api_models import CrawlPolicy
from app.crawl.job_store import CrawlJobState, FrontierEntry, JobStatus
from app.crawl.policy import read_policy
from app.settings import CrawlerSettings
from tests._job_harnesses import make_job_store, raw_job_row, write_row_policy
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier, job_store


def test_jobstore_create_and_load(job_store):
    job = job_store.create("http://example.com/", {"strategies": ["HTTP_DECLARED_CRAWLER"]})

    assert job.job_id
    assert job.root_url == "http://example.com"
    assert job.status == JobStatus.PENDING
    assert job.frontier == [FrontierEntry(url="http://example.com", depth=0)]
    assert job.visited == set()

    loaded = job_store.load(job.job_id)
    assert loaded is not None
    assert loaded.job_id == job.job_id
    assert loaded.root_url == job.root_url
    assert loaded.status == JobStatus.PENDING
    assert loaded.frontier == job.frontier


def test_jobstore_save_updates_state(job_store):
    job = job_store.create("http://example.com/", CrawlPolicy())
    job.frontier = ["http://example.com/page2"]
    job.visited = {"http://example.com/"}
    job.results = [{"url": "http://example.com/", "status": "success"}]
    job.stats = {"total_discovered": 2, "total_crawled": 1, "total_failed": 0}
    job_store.save(job)

    loaded = job_store.load(job.job_id)
    # DATA plane flows through save...
    assert loaded.frontier == [FrontierEntry(url="http://example.com/page2", depth=0)]
    assert loaded.visited == {"http://example.com"}
    assert len(loaded.results) == 1
    assert loaded.stats["total_crawled"] == 1
    # ...but the STATUS plane moves only via set_status (H3 separation).
    assert loaded.status == JobStatus.PENDING
    assert job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    assert job_store.load(job.job_id).status == JobStatus.RUNNING


def test_jobstore_list_jobs(job_store):
    job1 = job_store.create("http://site1.com/", CrawlPolicy())
    job2 = job_store.create("http://site2.com/", CrawlPolicy())
    job_store.set_status(job2.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    job_store.save(job2)

    jobs = job_store.list_jobs()
    assert len(jobs) == 2
    # Sorted by created_at desc
    assert jobs[0]["job_id"] == job2.job_id
    assert jobs[1]["job_id"] == job1.job_id
    assert jobs[0]["status"] == "running"
    assert jobs[1]["status"] == "pending"


def test_jobstore_delete(job_store):
    job = job_store.create("http://example.com/", CrawlPolicy())
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
    assert set(json_data["visited"]) == {"http://example.com"}

    loaded = CrawlJobState.from_json(json_data, host_policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
    assert loaded.job_id == "test123"
    assert loaded.status == JobStatus.RUNNING
    assert loaded.frontier == [
        FrontierEntry(url="http://example.com/a", depth=0),
        FrontierEntry(url="http://example.com/b", depth=0),
    ]
    assert loaded.visited == {"http://example.com"}
    assert len(loaded.results) == 1


def test_create_returns_full_uuid_hex():
    """HIGH #9: job IDs must not be short brute-forceable suffixes."""
    store = job_store(":memory:", policy=UNRESTRICTED_HOST_POLICY)
    job = store.create("https://example.com/", CrawlPolicy())
    assert len(job.job_id) == 32
    assert all(c in "0123456789abcdef" for c in job.job_id)
    loaded = store.load(job.job_id)
    assert loaded is not None
    assert loaded.job_id == job.job_id


def test_ids_are_unique():
    store = job_store(":memory:", policy=UNRESTRICTED_HOST_POLICY)
    ids = {store.create("https://example.com/", CrawlPolicy()).job_id for _ in range(50)}
    assert len(ids) == 50


class TestJobStoreLifecycle:
    def test_job_store_update(self, tmp_path):
        store = make_job_store(tmp_path)
        job = store.create("http://example.com/", CrawlPolicy())
        store.save(job)
        updated = store.load(job.job_id)
        assert (updated) is not None

    def test_job_store_delete(self, tmp_path):
        store = make_job_store(tmp_path)
        job = store.create("http://example.com/", CrawlPolicy())
        deleted = store.delete(job.job_id)
        assert deleted
        assert (store.load(job.job_id)) is None
        deleted = store.delete(job.job_id)
        assert not (deleted)


class TestJobStoreCorruptRow:
    def test_job_store_load_corrupt_row(self):
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_job_store(tmpdir)
            job = store.create("http://example.com/", CrawlPolicy())
            # Corrupt the policy JSON in the database
            import sqlite3

            conn = sqlite3.connect(str(pathlib.Path(tmpdir) / "jobs.db"))
            conn.execute("UPDATE crawl_jobs SET policy = ? WHERE job_id = ?", ("invalid json{", job.job_id))
            conn.commit()
            conn.close()
            # load should catch the JSON decode error and return None
            result = store.load(job.job_id)
            assert (result) is None


def _store(tmp_path=None):
    # Use in-memory SQLite to avoid disk I/O and lock contention (speed).
    return job_store(":memory:", policy=UNRESTRICTED_HOST_POLICY)


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
            job = store.create(f"http://example.com/{i}", CrawlPolicy())
            store.save(job)
            store.load(job.job_id)
            store.delete(job.job_id)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(worker, range(200)))

    locked = [e for e in errors if isinstance(e, sqlite3.OperationalError)]
    assert not locked, f"concurrent writes raised OperationalError: {locked[:3]}"
    assert not errors, f"unexpected errors: {errors[:3]}"


def _store_test_job_store_serialization(tmp_path):
    return make_job_store(tmp_path)


def test_save_survives_non_serializable_results(tmp_path):
    store = _store_test_job_store_serialization(tmp_path)
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
    store = _store_test_job_store_serialization(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert store.load("does-not-exist") is None
    assert not any("job_store_load_corrupt" in r.message for r in caplog.records)


def test_load_logs_warning_on_corruption(tmp_path, caplog):
    store = _store_test_job_store_serialization(tmp_path)
    # A job whose stored page history is not valid JSON. The history moved out
    # of the job row into its own table (migration 0003); what is under test is
    # unchanged, namely that a corrupt record surfaces a warning instead of
    # being silently swallowed into a missing job.
    store._conn.execute(
        """
        INSERT INTO crawl_jobs
        (job_id, root_url, policy, status, frontier,
         created_at, updated_at, error, stats)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "corrupt-id",
            "http://example.com/",
            "{}",
            "pending",
            "[]",
            0.0,
            0.0,
            None,
            "{}",
        ),
    )
    store._conn.execute(
        'INSERT INTO "crawl_job_results" (job_id, seq, payload) VALUES (?, ?, ?)',
        ("corrupt-id", 0, "{not valid json"),
    )
    store._conn.commit()
    with caplog.at_level(logging.WARNING):
        assert store.load("corrupt-id") is None
    assert any("job_store_load_corrupt" in r.message for r in caplog.records)


class TestJobStoreEdges:
    """Cover remaining branches in job_store.py"""

    def test_job_store_load_exception(self, tmp_path):
        store = make_job_store(tmp_path)
        conn = sqlite3.connect(str(store.db_path))
        conn.execute("DROP TABLE IF EXISTS jobs")
        conn.commit()
        conn.close()
        result = store.load("nonexistent")
        assert (result) is None

    def test_job_store_close(self, tmp_path):
        store = make_job_store(tmp_path)
        store._conn.close()
        store._conn.close()


def test_legacy_string_collections_load_with_depth_and_single_identity(tmp_path):
    store = make_job_store(tmp_path)
    root = "https://example.test/root"
    child = "https://example.test/child"
    job = store.create(root, {"limits": {"max_depth": 2}})
    with raw_job_row(store) as connection:
        connection.execute(
            "UPDATE crawl_jobs SET frontier = ?, in_flight = ? WHERE job_id = ?",
            (json.dumps([root, child]), json.dumps([root, child]), job.job_id),
        )

    loaded = store.load(job.job_id)

    assert loaded.frontier == []
    assert loaded.in_flight == [
        FrontierEntry(url="https://example.test/root", depth=0),
        FrontierEntry(url="https://example.test/child", depth=2),
    ]


def test_create_canonicalizes_root_and_initial_frontier(tmp_path):
    store = make_job_store(tmp_path)
    job = store.create("HTTPS://Example.test:443/", CrawlPolicy())
    loaded = store.load(job.job_id)

    assert loaded.root_url == "https://example.test"
    assert loaded.frontier == [FrontierEntry(url="https://example.test", depth=0)]


def test_load_resolves_unreadable_policy_to_the_declared_defaults(tmp_path):
    """A stored policy that cannot be read must not become a permissive one.

    This used to assert the opposite: a corrupt row sanitized to ``{}``, and
    ``{}`` read as ``respect_robots=False`` -- a whole-site job silently
    ignoring robots.txt, and ``max_depth``/``max_pages`` reading as 0. The
    sanitizer was the detection gap: it looked like a safety measure and was
    the mechanism. Unreadable now means the model's own defaults, and the
    assertion that matters is the protection, not the shape.
    """
    store = make_job_store(tmp_path)
    for policy_json in ('["corrupt"]', '{"limits":"corrupt","scope_regex":null}', '"not even a mapping"'):
        job = store.create("https://example.test/root", CrawlPolicy())
        write_row_policy(store, job.job_id, policy_json)

        loaded = store.load(job.job_id)
        limits = read_policy(loaded.policy).limits

        assert limits.respect_robots is True, policy_json
        assert limits.timeout_seconds == CrawlerSettings.from_mapping({}).default_timeout_seconds


def test_load_honors_an_explicitly_disabled_robots_limit(tmp_path):
    """A readable policy that opts out of robots.txt must keep opting out.

    The sibling of the unreadable-policy test, and it exists because that one
    was written to cover both cases at once -- which asserted that an explicit
    ``respect_robots: false`` resolved to ``True``. Resolution to defaults is
    what unreadable input gets; a caller's stated choice is a different
    decision, and silently upgrading it to the protection would make the
    opt-out untestable. The protection is asserted where it belongs: on
    unreadable input.
    """
    store = make_job_store(tmp_path)
    job = store.create("https://example.test/root", CrawlPolicy())
    write_row_policy(store, job.job_id, '{"limits":{"respect_robots":false}}')

    loaded = store.load(job.job_id)
    assert loaded is not None, "the row just written must load back"
    limits = read_policy(loaded.policy).limits

    assert limits.respect_robots is False
    assert limits.timeout_seconds == CrawlerSettings.from_mapping({}).default_timeout_seconds


def test_normalize_frontier_keeps_shallowest_duplicate_depth(tmp_path):
    store = make_job_store(tmp_path)
    job = store.create("https://example.test/root", {"limits": {"max_depth": 4}})
    job.frontier = [
        FrontierEntry("https://example.test/child", 4),
        FrontierEntry("https://example.test/child?utm_source=test", 1),
    ]
    job_store_data = job.to_json()

    loaded = CrawlJobState.from_json(
        job_store_data, host_policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier()
    )

    assert loaded.frontier == [FrontierEntry("https://example.test/child", 1)]


def test_cross_list_duplicate_keeps_shallowest_depth():
    from app.crawl.job_store import FrontierEntry

    loaded = CrawlJobState.from_json(
        {
            "job_id": "cross-list",
            "root_url": "https://example.test/root",
            "policy": {"limits": {"max_depth": 4}},
            "frontier": [{"url": "https://example.test/child", "depth": 1}],
            "in_flight": [{"url": "https://example.test/child", "depth": 3}],
        },
        host_policy=UNRESTRICTED_HOST_POLICY,
        tier=deployment_tier(),
    )

    assert loaded.frontier == []
    assert loaded.in_flight == [FrontierEntry("https://example.test/child", 1)]
