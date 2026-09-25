"""Whole-site job execution contract for ``/v1/jobs`` (H5 make-it-work).

The runner consumes the persisted frontier in persistent batches so every
transition (pause, resume, cancel, crash) leaves the store consistent.
These tests drive the real router + runner with a faked ``execute_crawl``
(the engine seam) — no network, no browsers.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from _job_harnesses import (
    _create_api_job,
    _fake_execute,
    _fake_execute_scripted,
    _make_app,
    _run_job_to_completed,
    _run_persisted_job,
    _run_scripted_job,
    _seed_job,
    _wait_status,
    _wait_until,
    make_job_store,
    post_api_job,
    run_job_control_test,
    run_seeded_scripted_job,
)
from _result_builders import _canned_result, _link, _scripted_result_map, _small_link_graph_results
from _timing_harnesses import await_until
from httpx import ASGITransport, AsyncClient

from app.api import job_runner
from app.crawl.job_store import FrontierEntry, JobStatus, JobStore
from app.settings import CrawlerSettings


@pytest.fixture(autouse=True)
def _fresh_runner():
    job_runner.reset_job_runner()
    yield
    # Cancel any lingering tasks so the suite stays order-independent.
    job_runner.reset_job_runner()


@pytest.fixture
def job_store(tmp_path):
    return make_job_store(tmp_path)


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

    async def test_crash_recovery_requeues_the_in_flight_batch(self, job_store):
        # Persisted state exactly as a hard kill during the first engine call leaves it:
        # root popped (frontier empty), recorded as in_flight, nothing visited, RUNNING.
        job = job_store.create("http://example.test/a", {})
        job.frontier = []
        job.in_flight = ["http://example.test/a"]
        job_store.save(job)
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")]))
        await job_runner.recover_and_start_pending_jobs(
            job_store, CrawlerSettings.from_mapping({}), asyncio.Event()
        )
        assert await _wait_status(job_store, job.job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job.job_id)
        assert job.visited == {"http://example.test/a"}, "in-flight batch was dropped, not re-crawled"
        assert job.in_flight == []
        assert job.stats["total_crawled"] == 1

    async def test_in_flight_requeue_survives_job_wide_page_cap(self, job_store):
        root = "http://example.test/a"
        discovered = "http://example.test/b"
        job = _seed_job(
            job_store,
            root,
            {"limits": {"max_depth": 2, "max_pages": 1}},
            frontier=[],
            in_flight=[root],
            running=True,
        )
        attempts: dict[str, int] = {}
        finished = await _run_persisted_job(
            job,
            job_store,
            _fake_execute_scripted(
                {root: [_canned_result(root, links=[discovered])]},
                attempts=attempts,
            ),
        )
        assert finished.status == JobStatus.COMPLETED
        assert finished.visited == {root}
        assert attempts == {root: 1}
        assert finished.frontier == []
        assert finished.stats["total_discovered"] == 0

    async def test_normal_completion_clears_in_flight(self, client, job_store):
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


    @pytest.mark.parametrize(
        ("url", "settings_values"),
        [
            ("https://blocked.test/secret", {"denied_hosts": "blocked.test"}),
            ("https://other.test/secret", {"allowed_hosts": "allowed.test"}),
            ("http://127.0.0.1/secret", {"allowed_hosts": "127.0.0.1"}),
        ],
    )
    async def test_rejected_job_is_not_persisted_or_started(self, job_store, monkeypatch, url, settings_values):
        from httpx import ASGITransport, AsyncClient

        from app.api.dependencies import get_settings
        from app.settings import CrawlerSettings

        app = _make_app(job_store)
        app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping(
            {f"BROWSER_CRAWLER_{key.upper()}": value for key, value in settings_values.items()}
        )
        started: list[str] = []
        monkeypatch.setattr(job_runner, "start_job", lambda job_id, *args: started.append(job_id))
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/jobs", json={"root_url": url})

        assert response.status_code == 422
        body = response.json()
        envelope = body if "code" in body else body["detail"]
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["errors"][0]["msg"] in {
            "host_denied",
            "host_not_allowed",
            "private_network_target",
        }
        assert job_store.list_jobs() == []
        assert started == []


class TestWholeSiteJobExecution:
    def test_engine_payload_builder_disables_recursion_and_preserves_limits(self, job_store):
        root = "http://example.test/a"
        original_policy = {
            "limits": {
                "max_depth": 2,
                "max_pages": 50,
                "max_discovered_links": 17,
                "max_rps": 2.5,
            },
            "scope_regex": "^https://example\\.test/",
        }
        job = job_store.create(root, original_policy)
        batch = [
            FrontierEntry(url=root, depth=0),
            FrontierEntry(url="http://example.test/b", depth=1),
        ]

        payload = job_runner._job_engine_payload(job, batch)

        assert payload == {
            "urls": [root, "http://example.test/b"],
            "policy": {
                "limits": {
                    "max_depth": 0,
                    "max_pages": 50,
                    "max_discovered_links": 17,
                    "max_rps": 2.5,
                },
                "scope_regex": "^https://example\\.test/",
            },
            "url": root,
            "headers": {},
            "request_profile": {},
        }
        assert job.policy == original_policy

    async def test_run_job_uses_engine_payload_builder(self, client, job_store):
        root = "http://example.test/a"
        captured: list[dict[str, Any]] = []
        base_execute = _fake_execute([_canned_result(root)])

        async def recording_execute(payload_dict, *, settings, shutdown_event):
            captured.append(payload_dict)
            return await base_execute(payload_dict, settings=settings, shutdown_event=shutdown_event)

        await _run_job_to_completed(
            client,
            job_store,
            root,
            recording_execute,
            {
                "limits": {
                    "max_depth": 2,
                    "max_pages": 50,
                    "max_discovered_links": 17,
                }
            },
        )

        assert len(captured) == 1
        limits = captured[0]["policy"]["limits"]
        assert limits["max_depth"] == 0
        assert limits["max_pages"] == 50
        assert limits["max_discovered_links"] == 17
        assert captured[0]["urls"] == [root]
        assert captured[0]["url"] == root
        assert captured[0]["headers"] == {}
        assert captured[0]["request_profile"] == {}

    async def test_max_pages_caps_the_whole_job_and_frontier_drains(self, client, job_store):
        job = await _run_job_to_completed(
            client,
            job_store,
            "http://example.test/a",
            _fake_execute(_small_link_graph_results()),
            {"limits": {"max_depth": 2, "max_pages": 2}},
        )

        assert job.status == JobStatus.COMPLETED
        assert job.visited == {"http://example.test/a", "http://example.test/b"}
        assert job.frontier == []
        assert job.in_flight == []
        assert job.stats["total_crawled"] == 2
        assert job.stats["total_discovered"] == 1

    async def test_max_pages_absent_keeps_the_whole_site_unbounded(self, client, job_store):
        captured: list[dict[str, Any]] = []
        base_execute = _fake_execute(_small_link_graph_results())

        async def recording_execute(payload_dict, *, settings, shutdown_event):
            captured.append(payload_dict)
            return await base_execute(payload_dict, settings=settings, shutdown_event=shutdown_event)

        job = await _run_job_to_completed(
            client,
            job_store,
            "http://example.test/a",
            recording_execute,
            {"limits": {"max_depth": 2}},
        )

        assert job.status == JobStatus.COMPLETED
        assert job.visited == {
            "http://example.test/a",
            "http://example.test/b",
            "http://example.test/c",
            "http://example.test/d",
        }
        assert job.frontier == []
        assert job.stats["total_crawled"] == 4
        assert job.stats["total_discovered"] == 3
        assert captured
        assert all(payload["policy"]["limits"]["max_depth"] == 0 for payload in captured)

    async def test_create_runs_to_completed(self, client, job_store):
        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            _fake_execute(
                [
                    _canned_result("http://example.test/a", links=["http://example.test/b"]),
                    _canned_result("http://example.test/b"),
                ]
            ),
        )

        status = await _wait_status(job_store, job_id, {"completed", "failed"})
        assert status == "completed"

        job = job_store.load(job_id)
        assert job.visited == {"http://example.test/a", "http://example.test/b"}
        assert job.frontier == []
        assert job.stats["total_crawled"] == 2
        assert job.stats["total_failed"] == 0
        # Discovered link b was crawled in a follow-up batch.
        assert len(job.results) == 2

    async def test_total_failed_counts_once_per_failed_item(self, client, job_store):
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

    async def test_failed_item_failing_to_build_counts_once(self, client, job_store):
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

    async def test_wide_fanout_crawls_every_discovered_url_once(self, client, job_store):
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

    async def test_transient_failure_is_requeued_then_succeeds(self, client, job_store):
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

    async def test_transient_retries_are_bounded_then_terminal(self, client, job_store):
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

    async def test_transient_requeue_survives_job_wide_page_cap(self, client, job_store):
        root = "http://example.test/a"
        admitted = "http://example.test/b"
        refused = "http://example.test/c"
        attempts: dict[str, int] = {}
        execute = _fake_execute_scripted(
            {
                root: [
                    _canned_result(root, status="failed", classification="network_error"),
                    _canned_result(root, links=[admitted, refused]),
                ]
            },
            attempts=attempts,
        )

        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            execute,
            {"limits": {"max_depth": 2, "max_pages": 2}},
        )

        assert job.status == JobStatus.COMPLETED
        assert attempts == {root: 2, admitted: 1}
        assert job.visited == {root, admitted}
        assert job.frontier == []
        assert job.stats["total_discovered"] == 1

    async def test_permanent_failure_is_terminal_without_requeue(self, client, job_store):
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

    async def test_frontier_expansion_uses_normalized_url(self, client, job_store):
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

        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            recording_execute,
        )
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job_id)
        assert "https://ok.test/abs" in job.visited
        assert "/rel" not in job.visited
        assert all("/rel" not in batch for batch in crawled)

    async def test_frontier_skips_unnormalizable_links(self, client, job_store):
        """A link without a validated normalized_url is never queue-eligible."""
        empty = {"url": "http://evil.test/x", "normalized_url": ""}
        bare = "http://legacy.test/string-link"
        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            _fake_execute([_canned_result("http://example.test/a", raw_links=[empty], links=[])]),
        )
        # The string link rides in a second result so a regression that queues
        # raw values is observable before completion too.
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"
        job = job_store.load(job_id)
        assert "http://evil.test/x" not in job.visited
        assert bare not in job.visited
        assert job.stats["total_discovered"] == 0

    async def test_pause_retains_frontier_and_resume_finishes(self, client, job_store):
        hang = asyncio.Event()
        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            _fake_execute(
                [
                    _canned_result(
                        "http://example.test/a", links=["http://example.test/b", "http://example.test/c"]
                    ),
                    _canned_result("http://example.test/b"),
                    _canned_result("http://example.test/c"),
                ],
                hang=hang,
            ),
        )
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

    async def test_cancel_persists_terminal_state(self, client, job_store):
        hang = asyncio.Event()
        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            _fake_execute(
                [_canned_result("http://example.test/a", links=["http://example.test/b"])], hang=hang
            ),
        )
        await _wait_status(job_store, job_id, {"running"})

        cancel_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
        assert cancel_resp.status_code == 200
        hang.set()

        # The in-flight batch is recorded before the runner honours the flip.
        job = await _wait_until(job_store, job_id, lambda j: j.stats.get("total_crawled", 0) >= 1)
        assert job.status.value == "cancelled"
        assert job.visited == {"http://example.test/a"}

    async def test_unknown_action_rejected(self, client, job_store):
        resp = await post_api_job(client, "http://example.test/a")
        job_id = resp.json()["job_id"]
        bad = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "explode"})
        assert bad.status_code == 422

    async def test_pause_non_running_conflicts(self, client, job_store):
        resp = await post_api_job(client, "http://example.test/a")
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"completed", "failed"})
        conflict = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert conflict.status_code == 409

    async def test_delete_running_job_cancels(self, client, job_store):
        hang = asyncio.Event()
        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            _fake_execute([_canned_result("http://example.test/a")], hang=hang),
        )
        await _wait_status(job_store, job_id, {"running"})

        del_resp = await client.delete(f"/v1/jobs/{job_id}")
        assert del_resp.status_code == 200
        hang.set()
        # The delete cancels the runner task; the wait is on the registry, not
        # on a pause that might or might not be long enough for the
        # cancellation to be delivered.
        await await_until(
            lambda: not job_runner.is_running(job_id),
            "the deleted job is still registered as running",
        )
        assert job_store.load(job_id) is None

    async def test_discovered_links_dedupe(self, client, job_store):
        # a discovers b and c; b discovers a again — visited dedupe wins.
        job_id = await _create_api_job(
            client,
            "http://example.test/a",
            _fake_execute(
                [
                    _canned_result(
                        "http://example.test/a", links=["http://example.test/b", "http://example.test/c"]
                    ),
                    _canned_result("http://example.test/b", links=["http://example.test/a"]),
                    _canned_result("http://example.test/c"),
                ]
            ),
        )
        await _wait_status(job_store, job_id, {"completed", "failed"})
        job = job_store.load(job_id)
        assert job.stats["total_crawled"] == 3
        assert job.visited == {"http://example.test/a", "http://example.test/b", "http://example.test/c"}


class TestRunnerLifecycle:
    async def test_shutdown_cancels_running_tasks(self, job_store):
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
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        recovered = await job_runner.recover_orphaned_running_jobs(job_store)
        assert recovered == 1
        assert job_store.load(job.job_id).status == JobStatus.PENDING

    async def test_recover_and_start_resumes_pending_jobs(self, job_store):
        import asyncio as _asyncio

        from app.settings import CrawlerSettings as _CS

        hang = _asyncio.Event()
        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")], hang=hang))
        # Orphan RUNNING row rewinds to PENDING...
        job = job_store.create("http://example.test/a", {})
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
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


class TestJobStateWaitContract:
    """The wait itself is the contract, so it is pinned like one.

    ``_wait_status`` used to poll the row against a 5.0s wall-clock deadline.
    That deadline is a starvation escape hatch wearing a deadline's clothes: it
    turned one red run under four agent sessions (62.9s user CPU inside a
    271.5s run) into a failing suite for a job that was progressing fine. These
    two tests hold the line so nobody quietly puts a poll-and-a-number back.
    """

    async def test_wait_status_rides_the_write_not_the_clock(self, job_store, monkeypatch):
        """The wait blocks until the control-plane write lands, then returns it.

        The runner's PENDING->RUNNING claim is held in its worker thread behind
        a gate, so the row is guaranteed to read ``pending`` for as long as the
        test wants it to. A waiter created before that claim can therefore not
        have completed -- there is nothing for it to have seen -- which is what
        makes "it was still blocked" an observation rather than a timing bet.
        Opening the gate lets the claim through and the assertion becomes: the
        wait returned the terminal status the job actually reached.
        """
        gate = threading.Event()
        real_set_status = job_store.set_status

        def gated_set_status(job_id, new, *, expected):
            if new is JobStatus.RUNNING:
                gate.wait(timeout=10)
            return real_set_status(job_id, new, expected=expected)

        monkeypatch.setattr(job_store, "set_status", gated_set_status)

        job_id = job_store.create("http://example.test/a", {}).job_id
        job_runner.install_execute_crawl(_fake_execute([_canned_result("http://example.test/a")]))
        assert job_store.status_bus is not None

        runner = asyncio.ensure_future(
            job_runner._run_job(
                job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )
        )
        try:
            waiter = asyncio.ensure_future(
                _wait_status(job_store, job_id, {"completed", "failed"})
            )
            # One turn is enough and not a guess: the waiter task cannot run at
            # all until the loop is handed back, and once it runs it reads a
            # `pending` row whose claim is still gated.
            await asyncio.sleep(0)
            assert not waiter.done(), "the wait returned before the job moved"
            assert job_store.load(job_id).status is JobStatus.PENDING

            gate.set()
            assert await waiter == "completed"
        finally:
            gate.set()
            await asyncio.wait_for(runner, timeout=10)

    async def test_wait_status_refuses_a_store_it_cannot_observe(self, job_store, tmp_path):
        """A store with no write bus is a hard error, never a silent poll.

        The fallback that would make this "work" -- poll the row until the state
        matches -- is exactly the implementation that raced a starved box, so
        the harness refuses the job instead of quietly reintroducing it.
        """
        from _job_harnesses import _wait_status as wait_status
        from _policy_factories import UNRESTRICTED_HOST_POLICY

        bare = JobStore(tmp_path / "bare.db", policy=UNRESTRICTED_HOST_POLICY)
        assert not hasattr(bare, "status_bus")
        job_id = bare.create("http://example.test/a", {}).job_id
        with pytest.raises(AssertionError, match="no status bus"):
            await wait_status(bare, job_id, {"completed"})


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
        # Set to completed via the control plane (save no longer writes status).
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        job_store.set_status(job.job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
        await run_job_control_test(app, job_store, job.job_id, "cancel", 409)
        job_store.delete(job.job_id)

    async def test_main_job_control_resume_not_paused(self):
        # H5: resume requires PAUSED; from PENDING it is a state conflict.
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "resume", 409)
        job_store.delete(job.job_id)


class TestWholeSiteAdmissionContract:
    @staticmethod
    def _policy(max_depth: int, max_pages: int, scope_regex: str | None = None) -> dict[str, Any]:
        policy: dict[str, Any] = {
            "limits": {
                "max_depth": max_depth,
                "max_pages": max_pages,
                "respect_robots": False,
            },
            "discovery": {"robots": True, "sitemaps": False, "feeds": True},
        }
        if scope_regex is not None:
            policy["scope_regex"] = scope_regex
        return policy

    async def test_depth_zero_does_not_expand(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        attempts: dict[str, int] = {}
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {root: [_canned_result(root, links=[child])]},
                attempts=attempts,
            ),
            self._policy(0, 10),
        )

        assert attempts == {root: 1}
        assert job.visited == {"https://example.test/root"}
        assert job.frontier == []
        assert job.stats["total_discovered"] == 0

    async def test_depth_one_expands_exactly_one_level(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        grandchild = "https://example.test/grandchild"
        attempts: dict[str, int] = {}
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {
                    root: [_canned_result(root, links=[child])],
                    child: [_canned_result(child, links=[grandchild])],
                },
                attempts=attempts,
            ),
            self._policy(1, 10),
        )

        assert attempts == {root: 1, child: 1}
        assert job.visited == {"https://example.test/root", "https://example.test/child"}
        assert job.frontier == []
        assert job.stats["total_discovered"] == 1

    async def test_scope_regex_excludes_link_before_page_slot(
        self, client, job_store
    ):
        root = "https://example.test/root"
        excluded = "https://example.test/excluded"
        included = "https://example.test/included"
        attempts: dict[str, int] = {}
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {root: [_canned_result(root, links=[excluded, included])]},
                attempts=attempts,
            ),
            self._policy(1, 2, r"^https://example\.test/included$"),
        )

        assert attempts == {root: 1, included: 1}
        assert job.visited == {"https://example.test/root", "https://example.test/included"}
        assert job.stats["total_discovered"] == 1

    async def test_robots_disallowed_link_does_not_consume_page_slot(
        self, client, job_store, monkeypatch
    ):
        root = "https://example.test/root"
        blocked = "https://example.test/blocked"
        included = "https://example.test/included"

        async def fetch_robots(fetcher, url, headers, timeout_seconds):
            return "User-agent: *\nDisallow: /blocked\n", 200

        monkeypatch.setattr("app.crawl.admission._fetch_and_decode", fetch_robots)
        attempts: dict[str, int] = {}
        policy = self._policy(1, 2)
        policy["limits"]["respect_robots"] = True
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {root: [_canned_result(root, links=[blocked, included])]},
                attempts=attempts,
            ),
            policy,
        )

        assert attempts == {root: 1, included: 1}
        assert job.visited == {"https://example.test/root", "https://example.test/included"}
        assert job.stats["total_discovered"] == 1

    async def test_legacy_oversized_frontier_truncates_at_pop(self, job_store):
        root = "https://example.test/root"
        second = "https://example.test/second"
        third = "https://example.test/third"
        job = _seed_job(job_store, root, self._policy(1, 2), frontier=[root, second, third])
        attempts: dict[str, int] = {}
        finished = await _run_persisted_job(
            job,
            job_store,
            _fake_execute_scripted(
                _scripted_result_map((root, second, third)),
                attempts=attempts,
            ),
        )
        assert attempts == {root: 1, second: 1}
        assert finished.visited == {"https://example.test/root", "https://example.test/second"}
        assert finished.frontier == []
        assert finished.stats["cap_truncated"] == 1

    async def test_in_flight_recovery_keeps_every_admitted_url(self, job_store):
        first = "https://example.test/first"
        second = "https://example.test/second"
        job = _seed_job(
            job_store,
            first,
            self._policy(1, 1),
            frontier=[],
            in_flight=[first, second],
            running=True,
        )
        attempts: dict[str, int] = {}
        finished = await _run_persisted_job(
            job,
            job_store,
            _fake_execute_scripted(
                _scripted_result_map((first, second)),
                attempts=attempts,
            ),
        )
        assert attempts == {first: 1, second: 1}
        assert finished.visited == {"https://example.test/first", "https://example.test/second"}
        assert finished.stats["cap_overcommitted"] == 1

    async def test_canonical_root_alias_counts_once(self, client, job_store):
        root = "https://example.test"
        self_alias = "https://example.test/"
        attempts: dict[str, int] = {}
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {root: [_canned_result(root, links=[self_alias])]},
                attempts=attempts,
            ),
            self._policy(1, 2),
        )

        assert attempts == {root: 1}
        assert job.visited == {"https://example.test"}
        assert len(job.results) == 1
        assert job.stats["total_discovered"] == 0

    async def test_cap_truncation_is_exposed_by_job_api(
        self, client, job_store
    ):
        root = "https://example.test/root"
        child = "https://example.test/child"
        job_id = await _create_api_job(
            client,
            root,
            _fake_execute([_canned_result(root, links=[child])]),
            self._policy(1, 1),
        )
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"

        response = await client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        assert response.json()["cap_truncated"] is True
        assert response.json()["stats"]["cap_truncated"] == 1

    @pytest.mark.parametrize("policy_json", ['["corrupt"]', '{"limits":"corrupt"}'])
    async def test_non_dict_policy_shapes_do_not_raise_mid_crawl(
        self, job_store, policy_json
    ):
        import sqlite3

        root = "https://example.test/root"
        job = _seed_job(job_store, root)
        connection = sqlite3.connect(job_store.db_path)
        connection.execute(
            "UPDATE crawl_jobs SET policy = ? WHERE job_id = ?",
            (policy_json, job.job_id),
        )
        connection.commit()
        connection.close()
        finished = await _run_persisted_job(
            job,
            job_store,
            _fake_execute([_canned_result(root)]),
        )
        assert finished.status == JobStatus.COMPLETED
        assert finished.visited == {"https://example.test/root"}


    async def test_job_seeds_sitemap_pages_through_shared_admission(
        self, client, job_store, monkeypatch
    ):
        root = "https://example.test/root"
        seed = "https://example.test/from-sitemap"
        policy = self._policy(1, 10)
        policy["discovery"]["sitemaps"] = True

        async def sitemap_candidates(root_url, context):
            assert root_url == root
            return [seed]

        monkeypatch.setattr(job_runner, "fetch_job_sitemap_candidates", sitemap_candidates)
        attempts: dict[str, int] = {}
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {root: [_canned_result(root)], seed: [_canned_result(seed)]},
                attempts=attempts,
            ),
            policy,
        )

        assert attempts == {root: 1, seed: 1}
        assert job.visited == {root, seed}
        assert job.frontier == []
        assert job.stats["total_discovered"] == 1

    async def test_analytics_and_document_links_do_not_consume_slots(
        self, client, job_store
    ):
        root = "https://example.test/root"
        analytics = "https://www.google-analytics.com/analytics.js"
        document = "https://example.test/report.pdf"
        included = "https://example.test/included"
        attempts: dict[str, int] = {}
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute_scripted(
                {root: [_canned_result(root, links=[analytics, document, included])]},
                attempts=attempts,
            ),
            self._policy(1, 2),
        )

        assert attempts == {root: 1, included: 1}
        assert job.visited == {root, included}
        assert job.stats["total_discovered"] == 1

    async def test_robots_crawl_delay_is_committed_before_admission(
        self, client, job_store, monkeypatch
    ):
        from app.scheduler import DomainScheduler

        root = "https://example.test/root"
        child = "https://example.test/child"
        committed: list[tuple[str, float]] = []

        async def allows(self, url: str) -> bool:
            return True

        async def crawl_delay(self, url: str) -> float:
            return 3.0

        def set_domain_min_delay(self, domain: str, seconds: float) -> None:
            committed.append((domain, seconds))

        monkeypatch.setattr("app.crawl.admission.RobotsGate.allows", allows)
        monkeypatch.setattr("app.crawl.admission.RobotsGate.crawl_delay", crawl_delay)
        monkeypatch.setattr(DomainScheduler, "set_domain_min_delay", set_domain_min_delay)
        policy = self._policy(1, 2)
        policy["limits"]["respect_robots"] = True
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute([_canned_result(root, links=[child])]),
            policy,
        )

        assert committed == [("example.test", 3.0)]
        assert job.visited == {root, child}


    async def test_in_flight_batch_discovering_each_other_does_not_double_crawl(
        self, job_store
    ):
        root = "https://example.test/root"
        child = "https://example.test/child"
        finished, attempts = await run_seeded_scripted_job(
            job_store,
            root,
            self._policy(1, 2),
            _scripted_result_map((root, child), links={root: [child]}),
            frontier=[root, child],
        )
        assert attempts == {root: 1, child: 1}
        assert finished.visited == {root, child}
        assert len(finished.results) == 2
        assert finished.stats["total_discovered"] == 0


    async def test_legacy_frontier_canonical_aliases_do_not_underfill_cap(
        self, job_store
    ):
        root = "https://example.test/root"
        root_alias = "https://example.test/root?utm_source=test"
        child = "https://example.test/child"
        finished, attempts = await run_seeded_scripted_job(
            job_store,
            root,
            self._policy(1, 2),
            _scripted_result_map((root, child)),
            frontier=[root, root_alias, child],
        )
        assert attempts == {root: 1, child: 1}
        assert finished.visited == {root, child}
        assert finished.stats["cap_truncated"] == 0


    async def test_chunked_in_flight_recovery_never_loses_admitted_urls(
        self, job_store
    ):
        urls = [f"https://example.test/page-{index}" for index in range(10)]
        finished, attempts = await run_seeded_scripted_job(
            job_store,
            urls[0],
            self._policy(1, 2),
            _scripted_result_map(urls),
            frontier=[],
            in_flight=urls,
            running=True,
        )
        assert attempts == {url: 1 for url in urls}
        assert finished.visited == set(urls)
        assert finished.in_flight == []
        assert finished.stats["cap_overcommitted"] == 1


    async def test_cross_list_alias_identity_is_single_work_item(self, job_store):
        root = "https://example.test/root"
        root_alias = "https://example.test/root?utm_source=legacy"
        child = "https://example.test/child"
        finished, attempts = await run_seeded_scripted_job(
            job_store,
            root,
            self._policy(1, 2),
            _scripted_result_map((root, child)),
            frontier=[child, root_alias],
            in_flight=[root],
            running=True,
        )
        assert attempts == {root: 1, child: 1}
        assert finished.visited == {root, child}
        assert len(finished.results) == 2
        assert finished.in_flight == []
        assert finished.frontier == []

    async def test_unbounded_batch_chunking_does_not_report_truncation(
        self, client, job_store
    ):
        root = "https://example.test/root"
        leaves = [f"https://example.test/leaf-{index}" for index in range(12)]
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute(
                [_canned_result(root, links=leaves)]
                + [_canned_result(leaf) for leaf in leaves]
            ),
            {"limits": {"max_depth": 1, "max_pages": 0}},
        )

        assert job.stats["cap_truncated"] == 0
        assert job.stats["total_crawled"] == 13
        assert len(job.results) == 13

    async def test_cap_truncated_is_exposed_by_list_and_control_responses(
        self, client, job_store
    ):
        job = job_store.create("https://example.test/root", self._policy(1, 1))
        job.stats["cap_truncated"] = 1
        job_store.save(job)
        assert job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        listed = await client.get("/v1/jobs")
        assert listed.status_code == 200
        summary = next(item for item in listed.json()["jobs"] if item["job_id"] == job.job_id)
        assert summary["cap_truncated"] is True

        controlled = await client.post(
            f"/v1/jobs/{job.job_id}/control",
            json={"action": "cancel"},
        )
        assert controlled.status_code == 200
        assert controlled.json()["cap_truncated"] is True


    async def test_duplicate_discovered_aliases_count_once_in_stats(
        self, client, job_store
    ):
        root = "https://example.test/root"
        child = "https://example.test/child"
        other = "https://example.test/other"
        job = await _run_job_to_completed(
            client,
            job_store,
            root,
            _fake_execute(
                [
                    _canned_result(
                        root,
                        links=[
                            f"{child}?utm_source=one",
                            f"{child}/",
                            other,
                        ],
                    ),
                    _canned_result(child),
                    _canned_result(other),
                ]
            ),
            {"limits": {"max_depth": 1, "max_pages": 3}},
        )

        assert job.stats["total_discovered"] == 2
        assert job.visited == {root, child, other}
        assert len(job.results) == 3
