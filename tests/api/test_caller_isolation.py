"""Caller-scoped reads: ownership is stored and foreign callers see nothing."""

from __future__ import annotations

from app.storage.job_store import JobStatus, JobStore
from app.storage.result_store import CrawlResultStore
from tests._policy_factories import job_store as make_store


def _seed_owned_job(store: JobStore, job_id: str = "crawl_owned", owner: str = "key_a") -> None:
    """One single-shot job owned by ``owner``; every scoped test seeds the same row."""
    store.create_single_shot(job_id, "http://example.com/", {}, owner=owner)


def test_single_shot_job_carries_its_owner(tmp_path):
    store = make_store(tmp_path / "jobs.db")

    _seed_owned_job(store)

    loaded = store.load("crawl_owned")
    assert loaded is not None
    assert loaded.owner == "key_a"


def test_job_created_without_owner_stays_anonymous(tmp_path):
    store = make_store(tmp_path / "jobs.db")

    job = store.create("http://example.com/", {})

    assert job.owner == ""
    assert store.load(job.job_id) is not None and store.load(job.job_id).owner == ""


def test_load_for_caller_hides_foreign_jobs(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    _seed_owned_job(store)

    assert store.load_for_caller("crawl_owned", "key_b") is None
    assert store.load_for_caller("crawl_owned", "") is None

    owned = store.load_for_caller("crawl_owned", "key_a")
    assert owned is not None and owned.job_id == "crawl_owned"


def test_load_for_caller_reads_legacy_anonymous_rows(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    store.create_single_shot("crawl_legacy", "http://example.com/", {})

    assert store.load_for_caller("crawl_legacy", "") is not None


def test_status_list_exists_and_delete_all_scope_to_owner(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    store.create_single_shot("crawl_a", "http://example.com/", {}, owner="key_a")
    store.create_single_shot("crawl_open", "http://example.com/", {})

    assert store.status_for_caller("crawl_a", "key_b") is None
    assert store.status_for_caller("crawl_a", "key_a") is not None
    assert store.exists_for_caller("crawl_a", "key_b") is False
    assert store.exists_for_caller("crawl_a", "key_a") is True
    assert {row["job_id"] for row in store.list_for_caller("key_a")} == {"crawl_a"}
    assert {row["job_id"] for row in store.list_for_caller("")} == {"crawl_open"}

    assert store.delete_for_caller("crawl_a", "key_b") is False
    assert store.exists_for_caller("crawl_a", "key_a") is True
    assert store.delete_for_caller("crawl_a", "key_a") is True
    assert store.load("crawl_a") is None


def test_saving_a_job_never_transfers_ownership(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    _seed_owned_job(store)

    loaded = store.load("crawl_owned")
    assert loaded is not None
    loaded.owner = "key_b"
    store.save(loaded)

    assert store.load("crawl_owned") is not None
    assert store.load("crawl_owned").owner == "key_a"
    assert store.load_for_caller("crawl_owned", "key_b") is None


def test_control_plane_moves_through_scoped_status(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    _seed_owned_job(store)

    assert store.set_status("crawl_owned", JobStatus.RUNNING, expected=JobStatus.PENDING) is True

    assert store.status_for_caller("crawl_owned", "key_a") is not None
    assert store.status_for_caller("crawl_owned", "key_b") is None


def test_result_store_scopes_payloads_by_owner(tmp_path):
    store = CrawlResultStore(tmp_path / "results.db")
    payload = {"request_id": "crawl_owned", "results": []}

    store.put("crawl_owned", payload, owner="key_a")

    assert store.get_for_caller("crawl_owned", "key_b") is None
    assert store.get_for_caller("crawl_owned", "") is None
    assert store.get_for_caller("crawl_owned", "key_a") == payload


def test_result_store_keeps_legacy_anonymous_reads(tmp_path):
    store = CrawlResultStore(tmp_path / "results.db")
    payload = {"request_id": "crawl_open", "results": []}
    store.put("crawl_open", payload)

    assert store.get("crawl_open") == payload
    assert store.get_for_caller("crawl_open", "") == payload
    assert store.get_for_caller("crawl_open", "key_a") is None


async def test_scoped_store_twins_round_trip_off_the_loop(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    results = CrawlResultStore(tmp_path / "results.db")
    await store.acreate_single_shot("crawl_owned", "http://example.com/", {}, owner="key_a")
    await results.aput("crawl_owned", {"request_id": "crawl_owned"}, owner="key_a")

    assert await store.aload_for_caller("crawl_owned", "key_b") is None
    assert await store.astatus_for_caller("crawl_owned", "key_a") is not None
    assert await store.aexists_for_caller("crawl_owned", "key_a") is True
    assert [row["job_id"] for row in await store.alist_for_caller("key_a")] == ["crawl_owned"]
    assert await results.aget_for_caller("crawl_owned", "key_a") == {"request_id": "crawl_owned"}
    assert await store.adelete_for_caller("crawl_owned", "key_a") is True
    assert await store.aload_for_caller("crawl_owned", "key_a") is None
