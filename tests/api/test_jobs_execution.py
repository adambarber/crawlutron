"""Whole-site job execution contract for ``/v1/jobs`` (H5 make-it-work).

The runner consumes the persisted frontier in persistent batches so every
transition (pause, resume, cancel, crash) leaves the store consistent.
These tests drive the real router + runner with a faked ``execute_crawl``
(the engine seam) — no network, no browsers.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import job_runner
from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.api.jobs_router import router as jobs_router
from app.crawl.job_store import CrawlJobState, JobStatus, JobStore
from app.settings import CrawlerSettings


def _link(url: str, source_url: str, *, normalized: str | None = None) -> dict:
    """Engine-shaped DiscoveredLink dump (raw strings fail build_unified_result).

    ``url`` is the RAW href as discovered in markup; ``normalized`` is the
    SafeUrl-policy-validated absolute URL the engine derives from it. They
    differ exactly the way extraction.py:634-635 can make them differ — the
    frontier must only ever see the normalized one.
    """
    return {
        "url": url,
        "normalized_url": normalized if normalized is not None else url,
        "text": "",
        "rel": [],
        "source_url": source_url,
        "host": (normalized or url).split("//", 1)[-1].split("/", 1)[0],
        "is_same_host": True,
    }


def _canned_result(
    url: str, *, status: str = "success", links: list[str] | None = None, raw_links: list[dict] | None = None
) -> dict:
    return {
        "original_url": url,
        "normalized_url": url,
        "final_url": url,
        "status": status,
        "classification": "rendered" if status == "success" else "site_error",
        "discovered_links": [_link(link, url) for link in links or []] + (raw_links or []),
    }


def _fake_execute(results: list[dict], *, hang: asyncio.Event | None = None):
    """Build an execute_crawl stand-in returning one result per submitted URL."""

    async def _execute(payload_dict, *, settings, shutdown_event):
        if hang is not None and not hang.is_set():
            # Block only the first invocation: gives the test a window to
            # land control requests while the first batch is in flight.
            await hang.wait()
        urls = payload_dict.get("urls") or [payload_dict.get("url")]
        by_url = {r["original_url"]: r for r in results}
        return {
            "results": [by_url.get(u, _canned_result(u, status="failed")) for u in urls],
            "submitted_count": len(urls),
            "policy_schema_version": "test",
        }

    return _execute


def _make_app(job_store: JobStore) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_router)
    shutdown = asyncio.Event()

    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: shutdown
    return app


async def _wait_status(job_store: JobStore, job_id: str, statuses: set[str], timeout: float = 5.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = job_store.load(job_id)
        if job is not None and job.status.value in statuses:
            return job.status.value
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {statuses}; last={job_store.load(job_id)}")


async def _wait_until(job_store: JobStore, job_id: str, predicate, timeout: float = 5.0) -> CrawlJobState:
    """Settle-wait on a row predicate — status alone can race the runner's
    next loop turn, which may still be recording an unblocked batch."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    job = job_store.load(job_id)
    while loop.time() < deadline:
        job = job_store.load(job_id)
        if job is not None and predicate(job):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"predicate never satisfied; last={job_store.load(job_id)}")


@pytest.fixture(autouse=True)
def _fresh_runner():
    job_runner.reset_job_runner()
    yield
    # Cancel any lingering tasks so the suite stays order-independent.
    job_runner.reset_job_runner()


@pytest.fixture
def job_store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


@pytest.fixture
def client(job_store):
    app = _make_app(job_store)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestWholeSiteJobExecution:
    async def test_create_runs_to_completed(self, client, job_store, real_sleeps):
        job_runner.install_execute_crawl(
            _fake_execute(
                [
                    _canned_result("http://example.test/a", links=["http://example.test/b"]),
                    _canned_result("http://example.test/b"),
                ]
            )
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        status = await _wait_status(job_store, job_id, {"completed", "failed"})
        assert status == "completed"

        job = job_store.load(job_id)
        assert job.visited == {"http://example.test/a", "http://example.test/b"}
        assert job.frontier == []
        assert job.stats["total_crawled"] == 2
        assert job.stats["total_failed"] == 0
        # Discovered link b was crawled in a follow-up batch.
        assert len(job.results) == 2

    async def test_total_failed_counts_once_per_failed_item(self, client, job_store, real_sleeps):
        # Batch 2 submits b and c, both fail: the per-item `failed` counter is
        # cumulative, so adding it inside the loop once reported 1+2=3.
        job_runner.install_execute_crawl(
            _fake_execute(
                [
                    _canned_result(
                        "http://example.test/a", links=["http://example.test/b", "http://example.test/c"]
                    )
                ]
            )
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
        job_id = resp.json()["job_id"]

        status = await _wait_status(job_store, job_id, {"completed", "failed"})
        assert status == "completed"
        job = job_store.load(job_id)
        assert job.stats["total_crawled"] == 3
        assert job.stats["total_failed"] == 2

    async def test_failed_item_failing_to_build_counts_once(self, client, job_store, real_sleeps):
        """M3: an item that fails to build AND carries status=failed counts once.

        build_unified_result requires original_url; an item with a resolvable
        URL but no original_url raises inside the try, and its
        status=="failed" must not add a second count for the same item.
        """

        async def _execute(payload_dict, *, settings, shutdown_event):
            return {
                "results": [
                    {
                        "normalized_url": "http://example.test/a",
                        "final_url": "http://example.test/a",
                        "status": "failed",
                        "classification": "site_error",
                    }
                ],
                "submitted_count": 1,
                "policy_schema_version": "test",
            }

        job_runner.install_execute_crawl(_execute)
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
        job_id = resp.json()["job_id"]

        status = await _wait_status(job_store, job_id, {"completed", "failed"})
        assert status == "completed"
        job = job_store.load(job_id)
        assert job.stats["total_crawled"] == 1
        assert job.stats["total_failed"] == 1

    async def test_wide_fanout_crawls_every_discovered_url_once(
        self, client, job_store, real_sleeps
    ):
        """M2: a 25-link fan-out across batches discovers and crawls each URL."""
        leaves = [f"http://example.test/l{i}" for i in range(25)]
        job_runner.install_execute_crawl(
            _fake_execute(
                [_canned_result("http://example.test/a", links=leaves)]
                + [_canned_result(url) for url in leaves]
            )
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
        job_id = resp.json()["job_id"]

        status = await _wait_status(job_store, job_id, {"completed", "failed"})
        assert status == "completed"
        job = job_store.load(job_id)
        assert job.stats["total_discovered"] == 25
        assert job.stats["total_crawled"] == 26
        assert len(job.results) == 26
        assert job.frontier == []

    async def test_frontier_expansion_uses_normalized_url(self, client, job_store, real_sleeps):
        """H1: the frontier must carry the validated URL, never the raw href.

        extraction.py can legitimately emit url="/rel" with normalized_url set
        to the resolved absolute URL; a runner that reads ``url`` re-queues
        unvalidated hrefs and dedupes them inconsistently against visited URLs.
        """
        divergent = _link("/rel", "http://example.test/a", normalized="https://ok.test/abs")
        install = _fake_execute(
            [
                _canned_result("http://example.test/a", raw_links=[divergent]),
            ]
        )
        crawled: list[list[str]] = []

        async def recording_execute(payload_dict, *, settings, shutdown_event):
            urls = payload_dict.get("urls") or [payload_dict.get("url")]
            crawled.append(list(urls))
            return await install(payload_dict, settings=settings, shutdown_event=shutdown_event)

        job_runner.install_execute_crawl(recording_execute)
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
        job_id = resp.json()["job_id"]
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job_id)
        assert "https://ok.test/abs" in job.visited
        assert "/rel" not in job.visited
        assert all("/rel" not in batch for batch in crawled)

    async def test_frontier_skips_unnormalizable_links(self, client, job_store, real_sleeps):
        """A link without a validated normalized_url is never queue-eligible."""
        empty = {"url": "http://evil.test/x", "normalized_url": ""}
        bare = "http://legacy.test/string-link"
        job_runner.install_execute_crawl(
            _fake_execute([_canned_result("http://example.test/a", raw_links=[empty], links=[])])
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
        job_id = resp.json()["job_id"]
        # The string link rides in a second result so a regression that queues
        # raw values is observable before completion too.
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"
        job = job_store.load(job_id)
        assert "http://evil.test/x" not in job.visited
        assert bare not in job.visited
        assert job.stats["total_discovered"] == 0

    async def test_pause_retains_frontier_and_resume_finishes(self, client, job_store, real_sleeps):
        hang = asyncio.Event()
        job_runner.install_execute_crawl(
            _fake_execute(
                [
                    _canned_result(
                        "http://example.test/a", links=["http://example.test/b", "http://example.test/c"]
                    ),
                    _canned_result("http://example.test/b"),
                    _canned_result("http://example.test/c"),
                ],
                hang=hang,
            )
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a"})
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"running"})

        pause_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert pause_resp.status_code == 200
        # Unblock the in-flight batch; the runner then records it and stops.
        hang.set()

        job = await _wait_until(
            job_store, job_id, lambda j: len(j.frontier) >= 1 or j.stats.get("total_crawled", 0) >= 1
        )
        assert job.status.value == "paused"
        assert len(job.frontier) >= 1  # unvisited discovered links retained

        resume_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "resume"})
        assert resume_resp.status_code == 200
        await _wait_status(job_store, job_id, {"completed"})
        job = job_store.load(job_id)
        assert job.visited == {"http://example.test/a", "http://example.test/b", "http://example.test/c"}

    async def test_cancel_persists_terminal_state(self, client, job_store, real_sleeps):
        hang = asyncio.Event()
        job_runner.install_execute_crawl(
            _fake_execute(
                [_canned_result("http://example.test/a", links=["http://example.test/b"])], hang=hang
            )
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a"})
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"running"})

        cancel_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
        assert cancel_resp.status_code == 200
        hang.set()

        # The in-flight batch is recorded before the runner honours the flip.
        job = await _wait_until(job_store, job_id, lambda j: j.stats.get("total_crawled", 0) >= 1)
        assert job.status.value == "cancelled"
        assert job.visited == {"http://example.test/a"}

    async def test_unknown_action_rejected(self, client, job_store, real_sleeps):
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a"})
        job_id = resp.json()["job_id"]
        bad = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "explode"})
        assert bad.status_code == 422

    async def test_pause_non_running_conflicts(self, client, job_store, real_sleeps):
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a"})
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"completed", "failed"})
        conflict = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert conflict.status_code == 409

    async def test_delete_running_job_cancels(self, client, job_store, real_sleeps):
        hang = asyncio.Event()
        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")], hang=hang))
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a"})
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"running"})

        del_resp = await client.delete(f"/v1/jobs/{job_id}")
        assert del_resp.status_code == 200
        hang.set()
        await asyncio.sleep(0.05)
        assert not job_runner.is_running(job_id)
        assert job_store.load(job_id) is None

    async def test_discovered_links_dedupe(self, client, job_store, real_sleeps):
        # a discovers b and c; b discovers a again — visited dedupe wins.
        job_runner.install_execute_crawl(
            _fake_execute(
                [
                    _canned_result(
                        "http://example.test/a", links=["http://example.test/b", "http://example.test/c"]
                    ),
                    _canned_result("http://example.test/b", links=["http://example.test/a"]),
                    _canned_result("http://example.test/c"),
                ]
            )
        )
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.test/a"})
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"completed", "failed"})
        job = job_store.load(job_id)
        assert job.stats["total_crawled"] == 3
        assert job.visited == {"http://example.test/a", "http://example.test/b", "http://example.test/c"}


class TestRunnerLifecycle:
    async def test_shutdown_cancels_running_tasks(self, job_store, real_sleeps):
        hang = asyncio.Event()
        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")], hang=hang))
        job = job_store.create("http://example.test/a", {})
        job_runner.start_job(job.job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event())
        await _wait_status(job_store, job.job_id, {"running"})
        await job_runner.shutdown_job_runner()
        assert not job_runner.is_running(job.job_id)
        job = job_store.load(job.job_id)
        assert job.status in {JobStatus.PAUSED, JobStatus.RUNNING}

    async def test_crash_orphan_recovered_to_pending(self, job_store):
        job = job_store.create("http://example.test/a", {})
        job.status = JobStatus.RUNNING
        job_store.save(job)

        recovered = job_runner.recover_orphaned_running_jobs(job_store)
        assert recovered == 1
        assert job_store.load(job.job_id).status == JobStatus.PENDING

    async def test_recover_and_start_resumes_pending_jobs(self, job_store, real_sleeps):
        import asyncio as _asyncio

        from app.settings import CrawlerSettings as _CS

        hang = _asyncio.Event()
        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")], hang=hang))
        # Orphan RUNNING row rewinds to PENDING...
        job = job_store.create("http://example.test/a", {})
        job.status = JobStatus.RUNNING
        job_store.save(job)
        # ...and a second row is already PENDING.
        job_store.create("http://example.test/b", {})

        started = await job_runner.recover_and_start_pending_jobs(
            job_store, _CS.from_mapping({}), _asyncio.Event()
        )
        assert started == 2
        assert job_runner.is_running(job.job_id)
        await job_runner.shutdown_job_runner()
