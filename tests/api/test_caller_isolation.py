"""Caller-scoped reads: ownership is stored and foreign callers see nothing."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.caller_auth import (
    check_page_quota,
    effective_cookie_namespace,
    identity_for_key,
)
from app.api.crawl_endpoint import get_crawl_result
from app.api.dependencies import get_job_store, get_result_store, get_settings, get_shutdown_event
from app.config.settings import CrawlerSettings
from app.models.caller_identity import CallerIdentity
from app.models.json_value import JsonValue
from app.storage.caller_key_store import CallerKeyStore
from app.storage.job_store import JobStatus, JobStore
from app.storage.result_store import CrawlResultStore
from tests._policy_factories import job_store as make_store


def _seed_owned_job(store: JobStore, job_id: str = "crawl_owned", owner: str = "key_a") -> None:
    """One single-shot job owned by ``owner``; every scoped test seeds the same row."""
    store.create_single_shot(job_id, "http://example.com/", {}, owner=owner)


def _complete_job(store: JobStore, job_id: str, payload: JsonValue, owner: str = "") -> None:
    """Seed one owned single-shot job and drive it to COMPLETED with ``payload``."""
    _seed_owned_job(store, job_id=job_id, owner=owner)
    done = store.load(job_id)
    assert done is not None
    done.results = [payload]
    store.save(done)
    store.set_status(job_id, JobStatus.COMPLETED, expected=JobStatus.PENDING)


def _isolation_stores(tmp_path: Path) -> tuple[JobStore, CrawlResultStore, CallerKeyStore]:
    """One isolated triple per test: jobs, results, and keys on scratch files."""
    return (
        make_store(tmp_path / "jobs.db"),
        CrawlResultStore(tmp_path / "results.db"),
        CallerKeyStore(tmp_path / "keys.db"),
    )


@contextlib.asynccontextmanager
async def _isolation_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, JobStore, CrawlResultStore, CallerKeyStore]]:
    """An HTTP client over isolated stores, yielding the client and the stores."""
    store, results, keys = _isolation_stores(tmp_path)
    app = _isolation_app(store, results, keys)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, results, keys


@contextlib.asynccontextmanager
async def _quota_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, JobStore, CrawlResultStore, CallerKeyStore]]:
    """An HTTP client whose app can run the crawl POST: settings and shutdown wired."""
    store, results, keys = _isolation_stores(tmp_path)
    app = _isolation_app(store, results, keys)
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: asyncio.Event()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, results, keys


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


def test_identity_for_key_carries_quotas(tmp_path):
    keys = CallerKeyStore(tmp_path / "keys.db")
    issued = keys.issue_key("agent-a", max_pages=25, max_concurrency=3)
    record = keys.verify_key(issued.secret)
    assert record is not None

    caller = identity_for_key(record)

    assert caller == CallerIdentity(
        caller_id=record.key_id,
        label="agent-a",
        authenticated=True,
        max_pages=25,
        max_concurrency=3,
    )


def test_effective_cookie_namespace_defaults_to_caller(tmp_path):
    keys = CallerKeyStore(tmp_path / "keys.db")
    record = keys.verify_key(keys.issue_key("agent-a").secret)
    assert record is not None
    caller = identity_for_key(record)
    anonymous = CallerIdentity()

    assert effective_cookie_namespace({}, caller) == caller.caller_id
    assert effective_cookie_namespace({}, anonymous) == ""
    assert effective_cookie_namespace({"cookie_namespace": "tenant-x"}, caller) == "tenant-x"


def test_page_quota_rejects_over_budget_crawls():
    import pytest
    from fastapi import HTTPException

    caller = CallerIdentity(caller_id="key_a", authenticated=True, max_pages=10)

    check_page_quota(caller, 10)
    check_page_quota(CallerIdentity(), 1000)

    with pytest.raises(HTTPException) as raised:
        check_page_quota(caller, 11)
    assert raised.value.status_code == 429


async def test_get_crawl_result_enforces_owner(tmp_path):
    import pytest
    from fastapi import HTTPException

    store = make_store(tmp_path / "jobs.db")
    results = CrawlResultStore(tmp_path / "results.db")
    _seed_owned_job(store)
    payload = {"request_id": "crawl_owned", "results": []}
    done = store.load("crawl_owned")
    assert done is not None
    done.results = [payload]
    store.save(done)
    store.set_status("crawl_owned", JobStatus.COMPLETED, expected=JobStatus.PENDING)
    owner = CallerIdentity(caller_id="key_a", authenticated=True)
    stranger = CallerIdentity(caller_id="key_b", authenticated=True)

    with pytest.raises(HTTPException) as raised:
        await get_crawl_result("crawl_owned", store, results, caller=stranger)
    assert raised.value.status_code == 404

    response = await get_crawl_result("crawl_owned", store, results, caller=owner)
    assert response.status_code == 200


def _isolation_app(job_store: JobStore, result_store: CrawlResultStore, keys: CallerKeyStore):
    """A test app with the crawl and key routers over isolated stores."""
    from app.api.crawl_router import router as crawl_router
    from app.api.keys_router import router as keys_router

    app = FastAPI()
    app.include_router(crawl_router)
    app.include_router(keys_router)
    app.state.caller_key_store = keys
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_result_store] = lambda: result_store
    return app


async def test_http_scoped_reads_and_key_issuance(tmp_path):
    async with _isolation_client(tmp_path) as (client, store, results, keys):
        first = await client.post("/v1/keys", json={"label": "agent-a"})
        assert first.status_code == 201, first.text
        other = await client.post("/v1/keys", json={"label": "agent-b"})
        assert other.status_code == 201, other.text
        listed = await client.get("/v1/keys")
        assert listed.status_code == 200, listed.text
        assert {row["label"] for row in listed.json()} == {"agent-a", "agent-b"}
        assert "secret" not in listed.text

        _complete_job(
            store,
            "crawl_owned",
            {
                "request_id": "crawl_owned",
                "results": [],
                "submitted_count": 0,
                "policy_schema_version": "",
                "success": True,
            },
            owner=first.json()["key_id"],
        )

        mine = {"X-API-Key": first.json()["secret"]}
        theirs = {"X-API-Key": other.json()["secret"]}
        assert (await client.get("/v1/crawl/crawl_owned")).status_code == 404
        assert (await client.get("/v1/crawl/crawl_owned", headers=theirs)).status_code == 404
        assert (await client.get("/v1/crawl/crawl_owned", headers=mine)).status_code == 200
        assert (await client.get("/v1/crawl/crawl_owned/trace", headers=mine)).status_code == 200


async def test_http_legacy_anonymous_rows_stay_readable(tmp_path):
    async with _isolation_client(tmp_path) as (client, store, results, keys):
        _complete_job(store, "crawl_open", {"request_id": "crawl_open"})
        assert (await client.get("/v1/crawl/crawl_open")).status_code == 200


def test_active_count_for_caller_counts_only_live_owned_jobs(tmp_path):
    store = make_store(tmp_path / "jobs.db")
    _seed_owned_job(store, job_id="crawl_running", owner="key_a")
    _seed_owned_job(store, job_id="crawl_other", owner="key_b")
    _seed_owned_job(store, job_id="crawl_done", owner="key_a")
    assert store.set_status("crawl_running", JobStatus.RUNNING, expected=JobStatus.PENDING) is True
    assert store.set_status("crawl_done", JobStatus.COMPLETED, expected=JobStatus.PENDING) is True

    assert store.active_count_for_caller("key_a") == 1
    assert store.active_count_for_caller("key_b") == 1
    assert store.active_count_for_caller("key_c") == 0


def test_concurrency_quota_rejects_a_full_caller():
    import pytest
    from fastapi import HTTPException

    from app.api.caller_auth import check_concurrency_quota

    caller = CallerIdentity(caller_id="key_a", authenticated=True, max_concurrency=1)

    check_concurrency_quota(caller, 0)
    check_concurrency_quota(CallerIdentity(), 100)

    with pytest.raises(HTTPException) as raised:
        check_concurrency_quota(caller, 1)
    assert raised.value.status_code == 429


async def test_http_concurrency_quota_rejects_a_full_caller(tmp_path):
    async with _quota_client(tmp_path) as (client, store, results, keys):
        issued = keys.issue_key("agent-a", max_concurrency=1)
        _seed_owned_job(store, job_id="crawl_busy", owner=issued.key_id)
        assert store.set_status("crawl_busy", JobStatus.RUNNING, expected=JobStatus.PENDING) is True
        refused = await client.post(
            "/v1/crawl",
            headers={"X-API-Key": issued.secret},
            json={"url": "http://example.com/", "async_mode": True},
        )
        assert refused.status_code == 429, refused.text


def test_crawl_metrics_carry_a_caller_dimension(tmp_path):
    from app.runtime.metrics import MetricsStore
    from app.storage.coordination_counters import CoordinationCounters

    metrics = MetricsStore(counters=CoordinationCounters(tmp_path / "counters.db"))

    metrics.observe_crawl(caller_id="key_a")
    metrics.observe_crawl(caller_id="key_a")
    metrics.observe_crawl(caller_id="key_b")
    metrics.observe_crawl()

    assert metrics.caller_counts == {"key_a": 2.0, "key_b": 1.0}
    exposition = metrics.as_prometheus()
    assert 'crawler_crawl_requests_total{caller="key_a"} 2.0' in exposition
    assert 'crawler_crawl_requests_total{caller="key_b"} 1.0' in exposition


async def test_http_page_quota_rejects_over_budget_crawls(tmp_path):
    async with _quota_client(tmp_path) as (client, store, results, keys):
        issued = keys.issue_key("agent-a", max_pages=1)
        refused = await client.post(
            "/v1/crawl",
            headers={"X-API-Key": issued.secret},
            json={
                "url": "http://example.com/",
                "async_mode": True,
                "policy": {"limits": {"max_pages": 50}},
            },
        )
        assert refused.status_code == 429, refused.text
        assert refused.json()["detail"]["code"] == "quota_exceeded"


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
