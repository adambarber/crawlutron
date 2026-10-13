"""Whole-site jobs run the real router and runner with a faked engine seam."""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.admission.policy import read_policy
from app.api import dependencies, job_runner, jobs_router
from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.storage.job_store import FrontierEntry, JobStatus
from tests._job_harnesses import (
    ScriptedExecute,
    _create_api_job,
    _make_app,
    _wait_status,
    assert_settled,
    job_limits,
    recording_execute,
    run_api_job,
    run_scripted_api_job,
    run_seeded_scripted_job,
)
from tests._policy_factories import canonical
from tests._result_builders import (
    GRAPH_CHILD,
    GRAPH_ROOT,
    _canned_result,
    _root_child_results,
    _small_link_graph_results,
    engine_result,
)


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
