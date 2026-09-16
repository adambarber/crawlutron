"""Whole-site job execution contract for ``/v1/jobs`` (H5 make-it-work).

The runner consumes the persisted frontier in persistent batches so every
transition (pause, resume, cancel, crash) leaves the store consistent.
These tests drive the real router + runner with a faked ``execute_crawl``
(the engine seam) — no network, no browsers.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from _testkit import run_job_control_test
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
    url: str,
    *,
    status: str = "success",
    links: list[str] | None = None,
    raw_links: list[dict] | None = None,
    classification: str | None = None,
) -> dict:
    return {
        "original_url": url,
        "normalized_url": url,
        "final_url": url,
        "status": status,
        "classification": classification or ("rendered" if status == "success" else "site_error"),
        "discovered_links": [_link(link, url) for link in links or []] + (raw_links or []),
    }


def _fake_execute_scripted(
    scripts: dict[str, list[dict]],
    *,
    attempts: dict[str, int] | None = None,
    hang: asyncio.Event | None = None,
):
    """Engine stand-in that replays a per-URL script, then repeats the last entry.

    Each URL maps to a list of canned results consumed one per engine call, so a
    test can make a URL fail transiently a few times then succeed. ``attempts``
    (when given) tallies how many times each URL is handed to the engine.
    """

    async def _execute(payload_dict, *, settings, shutdown_event):
        if hang is not None and not hang.is_set():
            await hang.wait()
        urls = payload_dict.get("urls") or [payload_dict.get("url")]
        results = []
        for u in urls:
            seq = scripts.get(u)
            if seq is None:
                entry = _canned_result(u)
            elif len(seq) > 1:
                entry = seq.pop(0)
            else:
                entry = seq[0]  # repeat the final scripted outcome
            if attempts is not None:
                attempts[u] = attempts.get(u, 0) + 1
            results.append(entry)
        return {
            "results": results,
            "submitted_count": len(urls),
            "policy_schema_version": "test",
        }

    return _execute


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


async def _run_job_to_completed(client, job_store, root_url: str, execute, policy: dict | None = None):
    """Drive one job to COMPLETED with a faked engine; return the loaded row.

    Shared prologue for execution-accounting tests. The per-test assertions
    on visited/stats/results stay in each test; only the drive-to-settled
    mechanics live here.
    """
    job_runner.install_execute_crawl(execute)
    resp = await client.post("/v1/jobs", json={"root_url": root_url, "policy": policy or {}})
    job_id = resp.json()["job_id"]
    status = await _wait_status(job_store, job_id, {"completed", "failed"})
    assert status == "completed"
    return job_store.load(job_id)


async def _run_scripted_job(
    client, job_store, root_url: str, sequence: list[dict]
) -> tuple[Any, dict[str, int]]:
    """Drive a whole-site job through a scripted per-URL sequence; return (row, attempts).

    Shared setup for the transient-retry accounting tests: the URL is popped,
    the engine returns ``sequence`` in order across attempts, and the test
    asserts on the final visited/results/stats/frontier state.
    """
    attempts: dict[str, int] = {}
    job = await _run_job_to_completed(
        client,
        job_store,
        root_url,
        _fake_execute_scripted({root_url: sequence}, attempts=attempts),
    )
    return job, attempts


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


class TestCrashRecovery:
    """A process killed mid-engine-call must not silently drop the in-flight batch.

    ``_run_job`` write-aheads the frontier pop (so the reload-each-iteration loop
    can't refetch the same batch), which means the persisted state right after a
    crash is: the batch removed from ``frontier`` but not yet recorded to
    ``visited``/``results``. Recovery must re-queue that batch and re-crawl it
    (at-least-once; visited + the content cache keep the re-fetch cheap), rather
    than observing an empty frontier and marking the job COMPLETED with 0 pages.
    """

    async def test_crash_recovery_requeues_the_in_flight_batch(self, job_store, real_sleeps):
        # Persisted state exactly as a hard kill during the first engine call leaves it:
        # root popped (frontier empty), recorded as in_flight, nothing visited, RUNNING.
        job = job_store.create("http://example.test/a", {})
        job.frontier = []
        job.in_flight = ["http://example.test/a"]
        job.status = JobStatus.RUNNING
        job_store.save(job)

        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")]))
        await job_runner.recover_and_start_pending_jobs(
            job_store, CrawlerSettings.from_mapping({}), asyncio.Event()
        )
        assert await _wait_status(job_store, job.job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job.job_id)
        assert job.visited == {"http://example.test/a"}, "in-flight batch was dropped, not re-crawled"
        assert job.in_flight == []

    async def test_normal_completion_clears_in_flight(self, client, job_store, real_sleeps):
        # A healthy run must not leave a stale in_flight that would double-crawl on a
        # later resume: the batch is cleared in the same save that records its results.
        job = await _run_job_to_completed(
            client,
            job_store,
            "http://example.test/a",
            _fake_execute(
                [
                    _canned_result("http://example.test/a", links=["http://example.test/b"]),
                    _canned_result("http://example.test/b"),
                ]
            ),
        )
        assert job.visited == {"http://example.test/a", "http://example.test/b"}
        assert job.in_flight == []


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
        job = await _run_job_to_completed(
            client,
            job_store,
            "http://example.test/a",
            _fake_execute(
                [
                    _canned_result(
                        "http://example.test/a", links=["http://example.test/b", "http://example.test/c"]
                    )
                ]
            ),
        )
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

        job = await _run_job_to_completed(client, job_store, "http://example.test/a", _execute)
        assert job.stats["total_crawled"] == 1
        assert job.stats["total_failed"] == 1

    async def test_wide_fanout_crawls_every_discovered_url_once(self, client, job_store, real_sleeps):
        """M2: a 25-link fan-out across batches discovers and crawls each URL."""
        leaves = [f"http://example.test/l{i}" for i in range(25)]
        job = await _run_job_to_completed(
            client,
            job_store,
            "http://example.test/a",
            _fake_execute(
                [_canned_result("http://example.test/a", links=leaves)]
                + [_canned_result(url) for url in leaves]
            ),
        )
        assert job.stats["total_discovered"] == 25
        assert job.stats["total_crawled"] == 26
        assert len(job.results) == 26
        assert job.frontier == []


class TestTransientFailureRequeue:
    """A page that can't be reached for a *transient* reason (network/dns/tls/
    timeout/browser crash) must not be counted done or drain the frontier — it is
    re-queued for a bounded number of retries, while a *permanent* failure
    (not_found/site_error/gone) is recorded terminal immediately. Otherwise a
    whole-site job whose links fail while the network is down would mark itself
    COMPLETED having crawled only the seed."""

    _CAP = 3  # MAX_URL_TRANSIENT_ATTEMPTS

    async def test_transient_failure_is_requeued_then_succeeds(self, client, job_store, real_sleeps):
        root = "http://example.test/a"
        job, attempts = await _run_scripted_job(
            client,
            job_store,
            root,
            [
                _canned_result(root, status="failed", classification="network_error"),
                _canned_result(root, status="failed", classification="dns_error"),
                _canned_result(root, status="success"),
            ],
        )
        # The two early failures must not have finalized the page; the crawl
        # retried and the single committed outcome is the eventual success.
        assert job.status == JobStatus.COMPLETED
        assert attempts[root] == 3
        assert job.visited == {root}
        assert len(job.results) == 1
        assert job.results[0]["status"] == "success"
        assert job.stats["total_crawled"] == 1
        assert job.stats["total_failed"] == 0

    async def test_transient_retries_are_bounded_then_terminal(self, client, job_store, real_sleeps):
        root = "http://example.test/a"
        job, attempts = await _run_scripted_job(
            client,
            job_store,
            root,
            [_canned_result(root, status="failed", classification="network_error")],
        )
        # A permanently-flaky page is attempted exactly the cap (not forever, not
        # once), then recorded as a terminal failure so the whole-site job can end.
        assert job.status == JobStatus.COMPLETED
        assert attempts[root] == self._CAP
        assert job.visited == {root}
        assert len(job.results) == 1
        assert job.results[0]["status"] == "failed"
        assert job.results[0]["error"] == "retries_exhausted"
        assert job.stats["total_failed"] == 1
        assert job.frontier == []

    async def test_permanent_failure_is_terminal_without_requeue(self, client, job_store, real_sleeps):
        root = "http://example.test/a"
        job, attempts = await _run_scripted_job(
            client,
            job_store,
            root,
            [_canned_result(root, status="failed", classification="not_found")],
        )
        # "Page is gone" is an answer, not a transient blip: recorded once, not retried.
        assert job.status == JobStatus.COMPLETED
        assert attempts[root] == 1
        assert job.visited == {root}
        assert job.results[0]["status"] == "failed"
        assert job.results[0]["classification"] == "not_found"
        assert job.stats["total_failed"] == 1

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


class TestWholeSiteRedirectedUrlResolution:
    """whole-site results are keyed by final_url but looked up by the
    pre-redirect frontier URL, so a redirected page misses -> "transient" ->
    re-queued forever, then counted failed and dropped: its content never
    reaches job.results.
    """

    async def test_frontier_url_resolves_via_final_url(self, job_store, monkeypatch):
        frontier_url = "http://example.com/page"
        final_url = "https://example.com/page/"  # http->https + trailing slash

        job = job_store.create(frontier_url, {"limits": {"max_depth": 1}})
        job_id = job.job_id

        call_count = {"n": 0}

        async def fake_execute_crawl(payload, *, settings=None, shutdown_event=None, **kwargs):
            call_count["n"] += 1
            # The engine reports the item keyed by its own URLs; the frontier
            # URL is the ORIGINAL (pre-redirect) one.
            return {
                "results": [
                    {
                        "original_url": frontier_url,
                        "normalized_url": frontier_url,
                        "final_url": final_url,
                        "status": "success",
                        "classification": "rendered",
                        "selected_strategy": "HTTP_DECLARED_CRAWLER",
                        "text": "page content",
                        "markdown": "page content",
                        "html": "<html><body>page content</body></html>",
                        "extraction_method": "http_html",
                        "quality_score": 1.0,
                    }
                ]
            }

        monkeypatch.setattr("app.api.job_runner._execute_crawl", fake_execute_crawl)

        from app.api.job_runner import _run_job

        await _run_job(job_id, job_store, MagicMock(), asyncio.Event())

        finished = job_store.load(job_id)
        assert finished is not None
        # The content must land in job.results exactly once (success), not be
        # re-queued until retries exhaust and then dropped.
        assert len(finished.results) == 1, f"results={finished.results}"
        assert finished.results[0]["status"] == "success", finished.results[0]
        assert finished.stats.get("total_failed", 0) == 0, finished.stats
        # And the frontier URL must be committed visited (not re-crawled).
        assert frontier_url in finished.visited, finished.visited


class TestJobControlTransitions:
    async def test_main_job_control_invalid_action(self):
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "invalid_action", 422)
        job_store.delete(job.job_id)

    async def test_main_job_control_cancel_completed(self):
        # H5: cancel from a terminal state is a state conflict (409), not a
        # shape error (422) — the request shape is valid, the transition isn't.
        from app.crawl.job_store import JobStatus
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        # Set to completed
        job.status = JobStatus.COMPLETED
        job_store.save(job)
        await run_job_control_test(app, job_store, job.job_id, "cancel", 409)
        job_store.delete(job.job_id)

    async def test_main_job_control_resume_not_paused(self):
        # H5: resume requires PAUSED; from PENDING it is a state conflict.
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "resume", 409)
        job_store.delete(job.job_id)
