"""Whole-site job execution contract for ``/v1/jobs`` (make-it-work).

The runner consumes the persisted frontier in persistent batches so every
transition (pause, resume, cancel, crash) leaves the store consistent.
These tests drive the real router + runner with a faked ``execute_crawl``
(the engine seam) — no network, no browsers.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.admission.policy import read_policy
from app.api import dependencies, job_runner, jobs_router
from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.storage.job_store import FrontierEntry, JobStatus, JobStore
from tests._job_harnesses import (
    ScriptedExecute,
    _create_api_job,
    _make_app,
    _wait_status,
    _wait_until,
    assert_settled,
    drive_job,
    job_limits,
    post_api_job,
    recording_execute,
    run_api_job,
    run_job_control_test,
    run_scripted_api_job,
    run_seeded_scripted_job,
    single_shot_job,
    single_shot_orphan,
    submit_blocked_job,
)
from tests._policy_factories import canonical
from tests._result_builders import (
    GRAPH_CHILD,
    GRAPH_ROOT,
    _canned_result,
    _flaky,
    _link,
    _root_child_results,
    _root_fans_out,
    _small_link_graph_results,
    engine_result,
)
from tests._timing_harnesses import await_until


@pytest.fixture(autouse=True)
def _fresh_runner():
    job_runner.reset_job_runner()
    yield
    # Cancel any lingering tasks so the suite stays order-independent.
    job_runner.reset_job_runner()


def test_every_job_endpoint_declares_its_providers_through_an_alias():
    """No job endpoint wires a provider as an invisible ``= Depends(...)`` default.

    Read off the registered routes, so a new endpoint is covered by declaring
    itself. A default is invisible in a signature -- it does not read as a
    requirement, and it is the shape that let two hand-written dependency lists
    (one per endpoint) look equivalent without being checked against anything.
    """
    aliases = {value for name, value in vars(dependencies).items() if name.endswith("Dep")}
    assert aliases, "app.api.dependencies declares no provider aliases"

    for route in jobs_router.router.routes:
        # An APIRouter registers APIRoutes, but the attribute holding them is typed as BaseRoute, which
        # declares no ``endpoint`` -- so the route type is the fact, and it is asserted rather than assumed.
        assert isinstance(route, APIRoute), f"the jobs router registered a {type(route).__name__}"
        for name, param in inspect.signature(route.endpoint, eval_str=True).parameters.items():
            where = f"{route.endpoint.__name__}.{name}"
            assert param.default is inspect.Parameter.empty, (
                f"{where} uses an invisible default; a provider belongs in the annotation"
            )
            for meta in getattr(param.annotation, "__metadata__", ()):
                # ``Depends()`` with no argument carries no callable, so there is no dependency to name an
                # alias for.
                if isinstance(meta, DependsParam) and meta.dependency is not None:
                    assert any(param.annotation is alias for alias in aliases), (
                        f"{where} wires {meta.dependency.__name__} outside a declared alias"
                    )


def test_creating_and_controlling_a_job_need_the_same_providers():
    """The identity claim behind the shared aliases, checked against the signatures.

    ``create_job`` and ``control_job`` were two hand-written copies of the same
    triple; they now name the same three aliases. If a fourth provider ever
    becomes necessary for one of them and not the other, this is what says so.
    """

    def _providers(handler) -> dict[str, Callable[..., Any]]:
        declared: dict[str, Callable[..., Any]] = {}
        # Eval_str: the router module defers its annotations (PEP 563), so the alias objects are only
        # reachable after the string is resolved.
        for name, param in inspect.signature(handler, eval_str=True).parameters.items():
            for meta in getattr(param.annotation, "__metadata__", ()):
                if isinstance(meta, DependsParam) and meta.dependency is not None:
                    declared[name] = meta.dependency
        return declared

    created = _providers(jobs_router.create_job)
    controlled = _providers(jobs_router.control_job)

    assert created == controlled
    assert set(created.values()) == {get_job_store, get_settings, get_shutdown_event}


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
        # Persisted state exactly as a hard kill during the first engine call leaves it: root popped (frontier
        # empty), recorded as in_flight, nothing visited, RUNNING.
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job.frontier = []
        job.in_flight = [FrontierEntry(url=canonical(GRAPH_ROOT), depth=0)]
        job_store.save(job)
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(GRAPH_ROOT)]))
        await job_runner.recover_and_start_pending_jobs(
            job_store, CrawlerSettings.from_mapping({}), asyncio.Event()
        )
        assert await _wait_status(job_store, job.job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job.job_id)
        assert job.visited == {canonical(GRAPH_ROOT)}, "in-flight batch was dropped, not re-crawled"
        assert job.in_flight == []
        assert job.stats["total_crawled"] == 1

    async def test_in_flight_requeue_survives_job_wide_page_cap(self, job_store):
        root = GRAPH_ROOT
        discovered = GRAPH_CHILD
        engine = ScriptedExecute({root: [_canned_result(root, links=[discovered])]})
        finished = await run_seeded_scripted_job(
            job_store,
            root,
            {"limits": {"max_depth": 2, "max_pages": 1}},
            engine,
            frontier=[],
            in_flight=[root],
            running=True,
        )
        assert finished.status == JobStatus.COMPLETED
        assert finished.visited == {canonical(root)}
        assert engine.attempts == {root: 1}
        assert finished.frontier == []
        assert finished.stats["total_discovered"] == 0

    async def test_normal_completion_clears_in_flight(self, client, job_store):
        # A healthy run must not leave a stale in_flight that would double-crawl on a later resume: the batch
        # is cleared in the same save that records its results.
        job = await run_scripted_api_job(client, job_store, GRAPH_ROOT, results=_root_child_results())
        assert job.visited == {canonical(GRAPH_ROOT), canonical(GRAPH_CHILD)}
        assert job.in_flight == []

    @pytest.mark.parametrize(
        ("url", "settings_values"),
        [
            ("https://blocked.test/secret", {"denied_hosts": "blocked.test"}),
            ("https://other.test/secret", {"allowed_hosts": "allowed.test"}),
            ("http://127.0.0.1/secret", {"allowed_hosts": "127.0.0.1"}),
        ],
    )
    async def test_rejected_job_is_not_persisted_or_started(
        self, job_store, monkeypatch, url, settings_values
    ):

        from app.api.dependencies import get_settings
        from app.config.settings import CrawlerSettings

        app = _make_app(job_store)
        app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping(
            {f"CRAWLUTRON_{key.upper()}": value for key, value in settings_values.items()}
        )
        started: list[str] = []
        monkeypatch.setattr(job_runner, "start_job", lambda job_id, *args: started.append(job_id))
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")

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
        root = GRAPH_ROOT
        # The limit block is stated once and the one field this test is about is the only thing that varies.

        def limits(*, max_depth: int = 2) -> dict:
            return job_limits(
                max_depth=max_depth,
                max_pages=50,
                max_discovered_links=17,
                max_rps=2.5,
                scope_regex=r"^https://example\.test/",
            )

        job = job_store.create(root, limits())
        batch = [
            FrontierEntry(url=canonical(GRAPH_ROOT), depth=0),
            FrontierEntry(url=canonical(GRAPH_CHILD), depth=1),
        ]

        payload = job_runner._job_engine_payload(job, batch)
        assert payload == {
            "urls": [root, GRAPH_CHILD],
            # Depth zero and nothing else moved: this builder exists to stop the per-call max_depth from
            # re-expanding a whole-site job's frontier.
            "policy": read_policy(limits(max_depth=0)),
            "url": root,
            "headers": {},
            "request_profile": {},
        }
        assert read_policy(job.policy) == read_policy(limits())

    async def test_run_job_uses_engine_payload_builder(self, client, job_store):
        root = GRAPH_ROOT
        captured: list[dict[str, Any]] = []
        engine = recording_execute(ScriptedExecute(results=[_canned_result(root)]), captured)

        await run_api_job(
            client,
            job_store,
            root,
            engine,
            {
                "limits": {
                    "max_depth": 2,
                    "max_pages": 50,
                    "max_discovered_links": 17,
                }
            },
        )

        assert len(captured) == 1
        limits = captured[0]["policy"].limits
        assert limits.max_depth == 0
        assert limits.max_pages == 50
        assert limits.max_discovered_links == 17
        assert captured[0]["urls"] == [root]
        assert captured[0]["url"] == root
        assert captured[0]["headers"] == {}
        assert captured[0]["request_profile"] == {}

    async def test_max_pages_caps_the_whole_job_and_frontier_drains(self, client, job_store):
        job = await run_api_job(
            client,
            job_store,
            GRAPH_ROOT,
            ScriptedExecute(results=_small_link_graph_results()),
            job_limits(max_depth=2, max_pages=2),
        )

        assert_settled(job, visited={GRAPH_ROOT, GRAPH_CHILD}, discovered=1, crawled=2)

    async def test_max_pages_absent_keeps_the_whole_site_unbounded(self, client, job_store):
        captured: list[dict[str, Any]] = []
        engine = recording_execute(ScriptedExecute(results=_small_link_graph_results()), captured)

        job = await run_api_job(
            client,
            job_store,
            GRAPH_ROOT,
            engine,
            job_limits(max_depth=2),
        )

        assert job.status == JobStatus.COMPLETED
        assert_settled(
            job,
            visited={GRAPH_ROOT, GRAPH_CHILD, "http://example.test/c", "http://example.test/d"},
            discovered=3,
            crawled=4,
        )
        assert captured
        assert all(payload["policy"].limits.max_depth == 0 for payload in captured)

    async def test_create_runs_to_completed(self, client, job_store):
        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            ScriptedExecute(results=_root_child_results()),
        )

        status = await _wait_status(job_store, job_id, {"completed", "failed"})
        assert status == "completed"

        job = job_store.load(job_id)
        assert_settled(job, visited={GRAPH_ROOT, GRAPH_CHILD}, crawled=2)
        assert job.stats["total_failed"] == 0
        # Discovered link b was crawled in a follow-up batch.
        assert len(job.results) == 2

    async def test_total_failed_counts_once_per_failed_item(self, client, job_store):
        # Batch 2 submits b and c, both fail: the per-item `failed` counter is cumulative, so adding it inside
        # the loop once reported 1+2=3.
        job = await run_scripted_api_job(
            client,
            job_store,
            GRAPH_ROOT,
            results=[_canned_result(GRAPH_ROOT, links=[GRAPH_CHILD, "http://example.test/c"])],
        )
        assert job.stats["total_crawled"] == 3
        assert job.stats["total_failed"] == 2

    async def test_failed_item_failing_to_build_counts_once(self, client, job_store):
        """An item that fails to build AND carries status=failed counts once.

        build_unified_result requires original_url; an item with a resolvable
        URL but no original_url raises inside the try, and its
        status=="failed" must not add a second count for the same item.
        """

        async def _execute(payload_dict, *, settings, shutdown_event):
            return {
                "results": [
                    engine_result(
                        GRAPH_ROOT,
                        "site_error",
                        status="failed",
                        final_url=GRAPH_ROOT,
                    )
                ],
                "submitted_count": 1,
                "policy_schema_version": "test",
            }

        job = await run_api_job(client, job_store, GRAPH_ROOT, _execute)
        assert job.stats["total_crawled"] == 1
        assert job.stats["total_failed"] == 1

    async def test_wide_fanout_crawls_every_discovered_url_once(self, client, job_store):
        """A 25-link fan-out across batches discovers and crawls each URL."""
        leaves = [f"http://example.test/l{i}" for i in range(25)]
        job = await run_scripted_api_job(
            client,
            job_store,
            GRAPH_ROOT,
            results=[_canned_result(GRAPH_ROOT, links=leaves)] + [_canned_result(u) for u in leaves],
        )
        assert job.stats["total_discovered"] == 25
        assert job.stats["total_crawled"] == 26
        assert len(job.results) == 26
        assert job.frontier == []

    async def test_discovered_links_dedupe(self, client, job_store):
        # A discovers b and c; b discovers a again — visited dedupe wins.
        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            ScriptedExecute(
                results=[
                    _canned_result(GRAPH_ROOT, links=[GRAPH_CHILD, "http://example.test/c"]),
                    _canned_result(GRAPH_CHILD, links=[GRAPH_ROOT]),
                    _canned_result("http://example.test/c"),
                ]
            ),
        )
        await _wait_status(job_store, job_id, {"completed", "failed"})
        job = job_store.load(job_id)
        assert job.stats["total_crawled"] == 3
        assert job.visited == {
            canonical(GRAPH_ROOT),
            canonical(GRAPH_CHILD),
            canonical("http://example.test/c"),
        }


class TestTransientFailureRequeue:
    """A page that can't be reached for a *transient* reason (network/dns/tls/
    timeout/browser crash) must not be counted done or drain the frontier — it is
    re-queued for a bounded number of retries, while a *permanent* failure
    (not_found/site_error/gone) is recorded terminal immediately. Otherwise a
    whole-site job whose links fail while the network is down would mark itself
    COMPLETED having crawled only the seed."""

    _CAP = 3  # MAX_URL_TRANSIENT_ATTEMPTS

    async def test_transient_failure_is_requeued_then_succeeds(self, client, job_store):
        root = GRAPH_ROOT
        engine = ScriptedExecute(_flaky(root, "network_error", "dns_error", lands_on=_canned_result(root)))
        job = await run_api_job(client, job_store, root, engine)
        # The two early failures must not have finalized the page; the crawl retried and the single committed
        # outcome is the eventual success.
        assert job.status == JobStatus.COMPLETED
        assert engine.attempts[root] == 3
        assert job.visited == {canonical(root)}
        assert len(job.results) == 1
        assert job.results[0]["status"] == "success"
        assert job.stats["total_crawled"] == 1
        assert job.stats["total_failed"] == 0

    async def test_transient_retries_are_bounded_then_terminal(self, client, job_store):
        root = GRAPH_ROOT
        engine = ScriptedExecute(_flaky(root, "network_error"))
        job = await run_api_job(client, job_store, root, engine)
        # A permanently-flaky page is attempted exactly the cap (not forever, not once), then recorded as a
        # terminal failure so the whole-site job can end.
        assert job.status == JobStatus.COMPLETED
        assert engine.attempts[root] == self._CAP
        assert job.visited == {canonical(root)}
        assert len(job.results) == 1
        assert job.results[0]["status"] == "failed"
        assert job.results[0]["error"] == "retries_exhausted"
        assert job.stats["total_failed"] == 1
        assert job.frontier == []

    async def test_transient_requeue_survives_job_wide_page_cap(self, client, job_store):
        root = GRAPH_ROOT
        admitted = GRAPH_CHILD
        refused = "http://example.test/c"
        engine = ScriptedExecute(
            _flaky(root, "network_error", lands_on=_canned_result(root, links=[admitted, refused]))
        )

        job = await run_api_job(
            client,
            job_store,
            root,
            engine,
            job_limits(max_depth=2, max_pages=2),
        )

        assert engine.attempts == {root: 2, admitted: 1}
        assert_settled(job, visited={root, admitted}, discovered=1)

    async def test_permanent_failure_is_terminal_without_requeue(self, client, job_store):
        root = GRAPH_ROOT
        engine = ScriptedExecute({root: [_canned_result(root, status="failed", classification="not_found")]})
        job = await run_api_job(client, job_store, root, engine)
        # "Page is gone" is an answer, not a transient blip: recorded once, not retried.
        assert job.status == JobStatus.COMPLETED
        assert engine.attempts[root] == 1
        assert job.visited == {canonical(root)}
        assert job.results[0]["status"] == "failed"
        assert job.results[0]["classification"] == "not_found"
        assert job.stats["total_failed"] == 1

    async def test_frontier_expansion_uses_normalized_url(self, client, job_store):
        """The frontier must carry the validated URL, never the raw href.

        extraction.py can legitimately emit url="/rel" with normalized_url set
        to the resolved absolute URL; a runner that reads ``url`` re-queues
        unvalidated hrefs and dedupes them inconsistently against visited URLs.
        """
        divergent = _link("/rel", GRAPH_ROOT, normalized="https://ok.test/abs")
        install = ScriptedExecute(
            results=[
                _canned_result(GRAPH_ROOT, raw_links=[divergent]),
            ]
        )
        crawled: list[list[str]] = []

        async def recording_execute(payload_dict, *, settings, shutdown_event):
            urls = payload_dict.get("urls") or [payload_dict.get("url")]
            crawled.append(list(urls))
            return await install(payload_dict, settings=settings, shutdown_event=shutdown_event)

        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            recording_execute,
        )
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job_id)
        assert canonical("https://ok.test/abs") in job.visited
        assert "/rel" not in job.visited
        assert all("/rel" not in batch for batch in crawled)

    async def test_frontier_skips_unnormalizable_links(self, client, job_store):
        """A link without a validated normalized_url is never queue-eligible."""
        empty = {"url": "http://evil.test/x", "normalized_url": ""}
        bare = "http://legacy.test/string-link"
        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            ScriptedExecute(results=[_canned_result(GRAPH_ROOT, raw_links=[empty], links=[])]),
        )
        # The string link rides in a second result so a regression that queues raw values is observable before
        # completion too.
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"
        job = job_store.load(job_id)
        assert "http://evil.test/x" not in job.visited
        assert bare not in job.visited
        assert job.stats["total_discovered"] == 0


class TestRunnerLifecycle:
    async def test_shutdown_cancels_running_tasks(self, job_store):
        hang = asyncio.Event()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(GRAPH_ROOT)], hang=hang))
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job_runner.start_job(job.job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event())
        await _wait_status(job_store, job.job_id, {"running"})
        await job_runner.shutdown_job_runner()
        assert not job_runner.is_running(job.job_id)
        job = job_store.load(job.job_id)
        assert job.status in {JobStatus.PAUSED, JobStatus.RUNNING}

    async def test_crash_orphan_recovered_to_pending(self, job_store):
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        recovered = await job_runner.recover_orphaned_running_jobs(job_store)
        assert recovered == 1
        assert job_store.load(job.job_id).status == JobStatus.PENDING

    async def test_paused_single_shot_job_is_not_resumed_as_a_whole_site_job(self):
        """The startup scan's kind filter was reading a key nothing populated.

        ``alist_jobs`` did not select ``kind``, so every summary answered
        ``None`` for it. The filter that reads that key -- the one meant to
        keep single-shot jobs out of the frontier-resuming runner -- therefore
        excluded nothing, and a single-shot job found PENDING at startup was
        handed to the whole-site runner and resumed from a frontier it does not
        have.

        The assertion is on what the scan refuses to launch, not on a status:
        the row is legitimately PENDING here, so the only thing that
        distinguishes a correct scan from the broken one is whether it starts
        work for it.
        """
        with single_shot_job(job_id="seeded-shot") as (store, job):
            started = await job_runner.recover_and_start_pending_jobs(
                store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )

            assert started == 0, "a SINGLE_SHOT job was handed to the whole-site runner"
            assert store.load(job.job_id).status == JobStatus.PENDING

    async def test_single_shot_orphan_is_failed_not_left_pending_forever(self):
        """A single-shot orphan is unrecoverable, and must be marked so.

        Recovery rewinds every RUNNING row to PENDING, but the startup path
        only resumes WHOLE_SITE jobs. A single-shot job killed mid-flight was
        therefore marked recoverable and then never recovered: it sat PENDING
        forever, and ``GET /v1/crawl/{id}`` answered ``202 pending`` from an
        endpoint that is otherwise authoritative. A polling client runs to its
        own timeout learning nothing.

        The two seams disagree about what "recoverable" means, so the fix
        belongs where the rewind is decided. The row is marked terminal at the
        moment the service concludes it cannot be resumed, which is the only
        point at which that conclusion is true.
        """
        with single_shot_orphan(job_id="orphan-shot") as (store, job):
            await job_runner.recover_orphaned_running_jobs(store)

            assert store.load(job.job_id).status == JobStatus.FAILED, (
                "a single-shot orphan is not resumable, so leaving it PENDING "
                "reports 'pending' to a client forever"
            )

    async def test_single_shot_orphan_does_not_become_a_runner_task(self):
        """The rewind alone must not make a single-shot job look startable.

        The terminal status above is what a client sees; this is the other
        half. If the FAILED write were ever the only thing missing, a
        single-shot row would still be PENDING at the moment the startup scan
        reads it, and the scan is what decides whether work is launched.
        """
        with single_shot_orphan(job_id="orphan-shot-2") as (store, job):
            await job_runner.recover_orphaned_running_jobs(store)
            started = await job_runner.recover_and_start_pending_jobs(
                store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )

            assert started == 0
            assert store.load(job.job_id).status == JobStatus.FAILED

    async def test_recover_and_start_resumes_pending_jobs(self, job_store):
        import asyncio as _asyncio

        from app.config.settings import CrawlerSettings as _CS

        hang = _asyncio.Event()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(GRAPH_ROOT)], hang=hang))
        # Orphan RUNNING row rewinds to PENDING...
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        # ...and a second row is already PENDING.
        job_store.create(GRAPH_CHILD, CrawlPolicy())

        started = await job_runner.recover_and_start_pending_jobs(
            job_store, _CS.from_mapping({}), _asyncio.Event()
        )
        assert started == 2
        assert job_runner.is_running(job.job_id)
        await job_runner.shutdown_job_runner()


class TestWholeSiteRedirectedUrlResolution:
    """Whole-site results are keyed by final_url but looked up by the
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
            # The engine reports the item keyed by its own URLs; the frontier URL is the ORIGINAL
            # (pre-redirect) one.
            return {
                "results": [
                    engine_result(
                        frontier_url,
                        "rendered",
                        final_url=final_url,
                        text="page content",
                        markdown="page content",
                        html="<html><body>page content</body></html>",
                        extraction_method="http_html",
                        quality_score=1.0,
                    )
                ]
            }

        monkeypatch.setattr("app.api.job_runner._execute_crawl", fake_execute_crawl)

        await drive_job(job_id, job_store, settings=MagicMock())

        finished = job_store.load(job_id)
        assert finished is not None
        # The content must land in job.results exactly once (success), not be re-queued until retries exhaust
        # and then dropped.
        assert len(finished.results) == 1, f"results={finished.results}"
        assert finished.results[0]["status"] == "success", finished.results[0]
        assert finished.stats.get("total_failed", 0) == 0, finished.stats
        # And the frontier URL must be committed visited (not re-crawled).
        assert canonical(frontier_url) in finished.visited, finished.visited


class TestStatusResponseShape:
    """The job status response is a projection of the status view, not a subset of it.

    The store persists a job's kind, the status projection selects it, and the two
    recovery seams read it back out of that same view. It was nevertheless absent
    from every status response the API returned: ``_build_job_status`` handed the
    view to the model with a ``**`` splat, and the model declares ``extra="ignore"``,
    so a key it did not declare was dropped without a word. No test asserted the
    response carried it, so no test could fail.

    Asserted here through the HTTP surface rather than against the model, because
    the client is what was being told something untrue.
    """

    async def test_status_response_carries_the_job_kind(self, client):
        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            ScriptedExecute(results=_root_child_results()),
        )

        response = await client.get(f"/v1/jobs/{job_id}")

        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "whole_site"

    async def test_single_shot_status_response_carries_its_own_kind(self, client, job_store):
        """The kind must come from the row, not from a per-endpoint constant.

        Both kinds read the same projection, so a response that hard-coded one
        would satisfy the whole-site case above while lying to every single-shot
        caller -- which is the half that decides resumption.
        """
        job_store.create_single_shot("shot-1", GRAPH_ROOT, CrawlPolicy())

        response = await client.get("/v1/jobs/shot-1")

        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "single_shot"


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

        job_id = job_store.create(GRAPH_ROOT, CrawlPolicy()).job_id
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(GRAPH_ROOT)]))
        assert job_store.status_bus is not None

        runner = drive_job(job_id, job_store)
        try:
            waiter = asyncio.ensure_future(_wait_status(job_store, job_id, {"completed", "failed"}))
            # One turn is enough and not a guess: the waiter task cannot run at all until the loop is handed
            # back, and once it runs it reads a `pending` row whose claim is still gated.
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
        from app.storage.job_store import JobStore
        from tests._job_harnesses import _wait_status as wait_status
        from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier

        bare = JobStore(tmp_path / "bare.db", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
        assert not hasattr(bare, "status_bus")
        job_id = bare.create(GRAPH_ROOT, CrawlPolicy()).job_id
        with pytest.raises(AssertionError, match="no status bus"):
            await wait_status(bare, job_id, {"completed"})


class TestJobControlOverRunner:
    """Control transitions observed on a live runner, not on a finished row.

    Each of these has to flip a control bit while the runner is mid-batch, so
    the engine is held and released at the moment the test means it -- see
    :func:`submit_blocked_job`.
    """

    @staticmethod
    async def _blocked(client, job_store, results):
        return await submit_blocked_job(client, job_store, GRAPH_ROOT, results)

    async def test_pause_retains_frontier_and_resume_finishes(self, client, job_store):
        job_id, release = await self._blocked(
            client, job_store, _root_fans_out(GRAPH_ROOT, GRAPH_CHILD, "http://example.test/c")
        )

        pause_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert pause_resp.status_code == 200
        # Unblock the in-flight batch; the runner then records it and stops.
        release.set()

        job = await _wait_until(
            job_store, job_id, lambda j: len(j.frontier) >= 1 or j.stats.get("total_crawled", 0) >= 1
        )
        assert job.status.value == "paused"
        assert len(job.frontier) >= 1  # unvisited discovered links retained

        resume_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "resume"})
        assert resume_resp.status_code == 200
        await _wait_status(job_store, job_id, {"completed"})
        job = job_store.load(job_id)
        assert job.visited == {
            canonical(GRAPH_ROOT),
            canonical(GRAPH_CHILD),
            canonical("http://example.test/c"),
        }

    async def test_cancel_persists_terminal_state(self, client, job_store):
        job_id, release = await self._blocked(client, job_store, _root_child_results())

        cancel_resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
        assert cancel_resp.status_code == 200
        release.set()

        # The in-flight batch is recorded before the runner honours the flip.
        job = await _wait_until(job_store, job_id, lambda j: j.stats.get("total_crawled", 0) >= 1)
        assert job.status.value == "cancelled"
        assert job.visited == {canonical(GRAPH_ROOT)}

    async def test_unknown_action_rejected(self, client, job_store):
        resp = await post_api_job(client, GRAPH_ROOT)
        job_id = resp.json()["job_id"]
        bad = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "explode"})
        assert bad.status_code == 422

    async def test_pause_non_running_conflicts(self, client, job_store):
        resp = await post_api_job(client, GRAPH_ROOT)
        job_id = resp.json()["job_id"]
        await _wait_status(job_store, job_id, {"completed", "failed"})
        conflict = await client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert conflict.status_code == 409

    async def test_delete_running_job_cancels(self, client, job_store):
        job_id, release = await self._blocked(client, job_store, [_canned_result(GRAPH_ROOT)])

        del_resp = await client.delete(f"/v1/jobs/{job_id}")
        assert del_resp.status_code == 200
        release.set()
        # The delete cancels the runner task; the wait is on the registry, not on a pause that might or might
        # not be long enough for the cancellation to be delivered.
        await await_until(
            lambda: not job_runner.is_running(job_id),
            "the deleted job is still registered as running",
        )
        assert job_store.load(job_id) is None


class TestJobControlTransitions:
    """A control action the job is not in a state to accept is a state conflict.

    The three cases differ only in the action and in the state the job is put
    into first, so the wiring around them -- the app, the store, the seed job,
    the cleanup -- is one helper rather than three copies of it. A copy is what
    made the "seed to a terminal state first" line the one most likely to be
    forgotten, and then a 409 test would have been asserting a 422.
    """

    @staticmethod
    async def _control(action: str, expected: int, *, seed_terminal: bool = False) -> None:
        from app.main import job_store
        from app.storage.job_store import JobStatus
        from tests._app_factory import service_app

        job = job_store.create("http://example.com/", CrawlPolicy())
        if seed_terminal:
            job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
            job_store.set_status(job.job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
        await run_job_control_test(service_app(), job_store, job.job_id, action, expected)
        job_store.delete(job.job_id)

    async def test_main_job_control_invalid_action(self):
        await self._control("invalid_action", 422)

    async def test_main_job_control_cancel_completed(self):
        # Cancel from a terminal state is a state conflict (409), not a shape error (422) — the request shape
        # is valid, the transition isn't.
        await self._control("cancel", 409, seed_terminal=True)

    async def test_main_job_control_resume_not_paused(self):
        # Resume requires PAUSED; from PENDING it is a state conflict.
        await self._control("resume", 409)


class TestEveryVisitedUrlHasAnEnvelope:
    """A job's results must account for every URL it marked visited.

    When the engine's batch came back without a result for a URL, the runner
    counted it in ``total_failed`` and moved on: the URL was in ``visited`` but
    nothing in ``results`` named it, so a job read as ``total_crawled: 2`` with a
    single result and no way to tell which page was missing. The batch's own
    shaping path already solved this -- ``_failed_unified_result`` carries the
    URL through and reports it as a terminal failure -- and its docstring says it
    mirrors the runner's semantics, which is exactly the coupling that let the
    runner drift from it.
    """

    async def test_a_url_the_engine_never_answered_still_gets_an_envelope(self, client, job_store):
        from tests._job_harnesses import _execution_payload, _execution_urls

        async def _execute(payload_dict, *, settings, shutdown_event):
            urls = await _execution_urls(payload_dict, None)
            answered = [u for u in urls if not u.raw.endswith("/gone")]
            results = [_canned_result(u, links=["http://example.test/gone"]) for u in answered]
            return _execution_payload(results, len(urls))

        job = await run_api_job(
            client,
            job_store,
            GRAPH_ROOT,
            _execute,
        )
        assert job.stats["total_crawled"] == 2
        assert job.stats["total_failed"] == 1
        # The point of the test: every visited URL is named in results.
        assert len(job.results) == 2, "a visited URL produced no result envelope"
        named = {r.get("normalized_url") or r.get("original_url") for r in job.results}
        assert "http://example.test/gone" in named
        failed = [r for r in job.results if r.get("normalized_url") == "http://example.test/gone"]
        assert failed and failed[0].get("status") == "failed"

    async def test_results_never_exceed_the_visited_set(self, client, job_store):
        """The accounting is a partition: each visited URL, exactly one envelope."""
        job = await run_scripted_api_job(client, job_store, GRAPH_ROOT, results=_root_child_results())
        assert len(job.results) == job.stats["total_crawled"]


class TestTheRunnerClaim:
    """The claim is the CAS: a previously read status never decides who runs the job."""

    @staticmethod
    def _seeded(job_store):
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job.frontier = [FrontierEntry(url=canonical(GRAPH_ROOT), depth=0)]
        job_store.save(job)
        return job

    @staticmethod
    def _paused_after_claim(job_store):
        """A seeded job claimed RUNNING then stopped PAUSED, engine installed.

        The flip is the claim the two paths below exercise: the row is PENDING,
        this process claims it, then stops it. What happens next differs per
        path; the state setup does not.
        """
        job = TestTheRunnerClaim._seeded(job_store)
        assert job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        assert job_store.set_status(job.job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)
        engine = ScriptedExecute(results=[_canned_result(GRAPH_ROOT)])
        job_runner.install_execute_crawl(engine)
        return job, engine

    async def test_a_row_already_running_is_left_alone_by_a_second_runner(self, job_store):
        # The create/recovery entrance path: whoever loaded a RUNNING row is not the owner, and an owner it
        # did not become means no work.
        job = self._seeded(job_store)
        assert job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        engine = ScriptedExecute(results=[_canned_result(GRAPH_ROOT)])
        job_runner.install_execute_crawl(engine)

        await drive_job(job.job_id, job_store)

        assert engine.attempts == {}, "a second runner claimed a job it never held"
        assert job_store.load(job.job_id).status is JobStatus.RUNNING

    async def test_resume_still_runs_the_row_it_flipped_itself(self, job_store):
        # The flip is the claim on this path, and only the process that performed it may run the job -- which
        # is what distinguishes it from the case above.
        job, _engine = self._paused_after_claim(job_store)

        outcome = await job_runner.resume_job(
            job.job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event()
        )

        assert outcome == "ok"
        assert await _wait_status(job_store, job.job_id, {"completed", "failed"}) == "completed"
        assert job_store.load(job.job_id).visited == {canonical(GRAPH_ROOT)}

    async def test_a_row_stopped_between_the_read_and_the_claim_is_not_crawled(self, job_store):
        # The runner read RUNNING; by the time it claimed, the row had stopped. A stale read must not outvote
        # what the store says now.
        job, engine = self._paused_after_claim(job_store)
        raced = _RunnerReadRunsBeforeTheStore(job_store)

        await drive_job(job.job_id, raced)

        assert engine.attempts == {}, "a runner crawled a job after it had been stopped"
        assert raced.load(job.job_id).status is JobStatus.PAUSED


class _RunnerReadRunsBeforeTheStore:
    """Store whose rows read as they were before this run's status flip: the
    read/claim window, with everything else delegating to real rows."""

    def __init__(self, inner: JobStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def aload(self, job_id: str):
        job = await self._inner.aload(job_id)
        return replace(job, status=JobStatus.RUNNING) if job is not None else None
