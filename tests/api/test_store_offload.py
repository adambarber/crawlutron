"""No sync store method runs on the event loop (the store-offload chokepoint).

``JobStore`` and ``CrawlResultStore`` are synchronous SQLite wrappers: every
public method holds a lock, reads or writes a row, and commits. On the event
loop that is not "a bit slow" -- a 3000-page job re-serializes its whole row on
every batch persist, so one save parks the loop long enough to delay liveness
for every other in-flight request, and ``/ready`` with it.

Most call sites already offloaded by hand (``asyncio.to_thread(job_store.save,
job)``), which is exactly the shape that decays: the next method someone adds
has no wrapper, and the discipline lives in the call site instead of in the
store. The invariant is therefore stated against the store's own surface -- each
public method has an ``a``-prefixed twin that runs the whole call off-loop --
and these tests drive the real endpoints and the real startup recovery with a
store that REFUSES to run its sync surface on the loop.

The refusal is a real subclass, not a mock: every method is the production one
and the guard sits in front of it, so a passing test is evidence about the
production code path rather than about the double.
"""

from __future__ import annotations

import asyncio
import functools
import inspect

import pytest
from _job_harnesses import _fake_execute, _make_app, _seed_job
from _policy_factories import UNRESTRICTED_HOST_POLICY
from _result_builders import _canned_result
from httpx import ASGITransport, AsyncClient

from app.api import job_runner
from app.crawl.job_store import JobStore
from app.crawl.result_store import CrawlResultStore
from app.settings import CrawlerSettings

_ROOT = "http://example.test/a"


def _refuse_on_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(
        f"sync store method {name} ran on the event loop; the async twin owns that call"
    )


class LoopGuardJobStore(JobStore):
    """The real store, with every sync entry point refusing to run on the loop."""

    def create(self, root_url, policy):
        _refuse_on_loop("JobStore.create")
        return super().create(root_url, policy)

    def create_single_shot(self, job_id, root_url, policy):
        _refuse_on_loop("JobStore.create_single_shot")
        return super().create_single_shot(job_id, root_url, policy)

    def save(self, job):
        _refuse_on_loop("JobStore.save")
        return super().save(job)

    def set_status(self, job_id, new_status, *, expected=None):
        _refuse_on_loop("JobStore.set_status")
        return super().set_status(job_id, new_status, expected=expected)

    def load(self, job_id):
        _refuse_on_loop("JobStore.load")
        return super().load(job_id)

    def delete(self, job_id):
        _refuse_on_loop("JobStore.delete")
        return super().delete(job_id)

    def list_jobs(self):
        _refuse_on_loop("JobStore.list_jobs")
        return super().list_jobs()

    def close(self):
        _refuse_on_loop("JobStore.close")
        return super().close()


class LoopGuardResultStore(CrawlResultStore):
    """The real result store, with its sync entry points refusing the loop."""

    def put(self, request_id, payload):
        _refuse_on_loop("CrawlResultStore.put")
        return super().put(request_id, payload)

    def get(self, request_id):
        _refuse_on_loop("CrawlResultStore.get")
        return super().get(request_id)

    def close(self):
        _refuse_on_loop("CrawlResultStore.close")
        return super().close()


@pytest.fixture
def loop_guard_store(tmp_path) -> LoopGuardJobStore:
    return LoopGuardJobStore(tmp_path / "jobs.db", policy=UNRESTRICTED_HOST_POLICY)


@pytest.fixture
def result_store(tmp_path) -> LoopGuardResultStore:
    return LoopGuardResultStore(tmp_path / "results.db")


@pytest.fixture
def jobs_client(loop_guard_store):
    app = _make_app(loop_guard_store)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _off_loop(func, *args, **kwargs):
    """Call a store method the way production must: off the loop.

    The test's own reads go through here too, and that is the point -- a store
    that forbids on-loop access also forbids it to the test, so no assertion can
    accidentally pass because the harness read the row the compliant way while
    production did not.
    """
    return await asyncio.to_thread(functools.partial(func, *args, **kwargs))


class TestJobEndpointsStayOffTheLoop:
    """Every ``/v1/jobs`` handler used to reach a sync method directly.

    ``create_job`` wrote the row, ``list_jobs`` read every row, ``get_job`` and
    ``control_job`` read one twice, ``delete_job`` deleted -- all on the loop
    that was supposed to be answering liveness probes.
    """

    async def test_the_whole_job_endpoint_surface_stays_off_the_loop(
        self, jobs_client, loop_guard_store
    ):
        # The crawl is held open across every assertion. Without it this test
        # raced its own background task: POST /v1/jobs starts the runner, the
        # fake engine returned instantly, and the job reached COMPLETED before
        # the cancel arrived -- so ``control`` answered 409 and the suite went
        # red at random under full-suite load while passing in isolation. A test
        # that only fails when the machine is busy is asserting on timing, not
        # on the off-the-loop behaviour it is named for.
        hang = asyncio.Event()
        job_runner.reset_job_runner()
        job_runner.install_execute_crawl(_fake_execute([_canned_result(_ROOT)], hang=hang))
        try:
            created = await jobs_client.post(
                "/v1/jobs",
                json={"root_url": _ROOT, "policy": {"limits": {"max_depth": 1}}},
            )
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]

            listed = await jobs_client.get("/v1/jobs")
            assert listed.status_code == 200
            assert job_id in {row["job_id"] for row in listed.json()["jobs"]}

            fetched = await jobs_client.get(f"/v1/jobs/{job_id}")
            assert fetched.status_code == 200, fetched.text

            controlled = await jobs_client.post(
                f"/v1/jobs/{job_id}/control", json={"action": "cancel"}
            )
            assert controlled.status_code == 200, controlled.text

            deleted = await jobs_client.delete(f"/v1/jobs/{job_id}")
            assert deleted.status_code == 200, deleted.text
            assert await _off_loop(loop_guard_store.load, job_id) is None
        finally:
            # Release the held crawl before the store fixture tears down, or the
            # background task outlives the loop and writes to a closed DB.
            hang.set()
            job_runner.reset_job_runner()


class TestStartupRecoveryStaysOffTheLoop:
    """Startup recovery runs inside the lifespan, so it has a loop to block.

    ``recover_orphaned_running_jobs`` rewound RUNNING rows and
    ``recover_and_start_pending_jobs`` listed every job and read each one back,
    all synchronously, on the loop that had just finished booting the service.
    """

    async def test_recovery_rewinds_and_starts_without_touching_the_loop(
        self, loop_guard_store
    ):
        from app.api.job_runner import recover_and_start_pending_jobs

        job_runner.reset_job_runner()
        job_runner.install_execute_crawl(_fake_execute([_canned_result(_ROOT)]))
        try:
            job = await _off_loop(
                _seed_job, loop_guard_store, _ROOT, {"limits": {"max_depth": 1}}
            )
            await recover_and_start_pending_jobs(
                loop_guard_store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )
            status = await _await_terminal_status(loop_guard_store, job.job_id)
            assert status == "completed"
        finally:
            job_runner.reset_job_runner()

    async def test_rewinding_a_crash_orphan_never_touches_the_loop(self, loop_guard_store):
        from app.api.job_runner import recover_orphaned_running_jobs

        job = await _off_loop(
            _seed_job, loop_guard_store, _ROOT, {"limits": {"max_depth": 1}}, running=True
        )
        rewound = await recover_orphaned_running_jobs(loop_guard_store)
        assert rewound == 1
        reloaded = await _off_loop(loop_guard_store.load, job.job_id)
        assert reloaded is not None and reloaded.status.value == "pending"


class TestSingleShotResultStoreStaysOffTheLoop:
    """``/v1/crawl/{request_id}`` read the result store, then the job store.

    Both are synchronous SQLite reads on the request path of a status poll -- the
    endpoint a client hits repeatedly while a background crawl runs.
    """

    async def test_reading_a_stored_single_shot_response_stays_off_the_loop(
        self, loop_guard_store, result_store
    ):
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(
            loop_guard_store.create_single_shot, "req-1", _ROOT, {"strategies": []}
        )
        stored = {
            "request_id": "req-1",
            "results": [],
            "submitted_count": 0,
            "policy_schema_version": "",
            "success": True,
        }
        await _off_loop(result_store.put, "req-1", stored)

        response = await get_crawl_result("req-1", loop_guard_store, result_store)
        assert response.request_id == "req-1"

    async def test_a_pending_single_shot_read_stays_off_the_loop(
        self, loop_guard_store, result_store
    ):
        from fastapi import HTTPException

        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(
            loop_guard_store.create_single_shot, "req-2", _ROOT, {"strategies": []}
        )
        with pytest.raises(HTTPException) as raised:
            await get_crawl_result("req-2", loop_guard_store, result_store)
        assert raised.value.status_code == 202


async def _await_terminal_status(store: JobStore, job_id: str) -> str:
    """Poll the status off-loop until it is terminal.

    The store forbids on-loop reads, so the harness cannot use the edge-driven
    ``_wait_status`` (it reads on the loop). The bound here is the same hang
    guard the job harness documents; a job that is progressing reaches a
    terminal status on its own, and one that is not should fail loudly.
    """
    for _ in range(int(30.0 / 0.005)):
        job = await _off_loop(store.load, job_id)
        if job is not None and job.status.value in {"completed", "failed"}:
            return job.status.value
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never reached a terminal status")


def test_the_guard_itself_bites():
    """A guard that cannot fail proves nothing, so prove it fires.

    ``asyncio.run`` gives a real running loop on this thread, which is the
    condition the guard exists to catch.
    """

    async def call_the_sync_surface() -> None:
        store = LoopGuardJobStore(":memory:", policy=UNRESTRICTED_HOST_POLICY)
        store.list_jobs()

    with pytest.raises(AssertionError, match="JobStore.list_jobs ran on the event loop"):
        asyncio.run(call_the_sync_surface())


def _sync_surface(store_class: type) -> set[str]:
    """The store's public SYNCHRONOUS methods -- the ones the guard must cover.

    The ``a``-prefixed twins are excluded deliberately: they are the compliant
    path, and covering them would be covering the fix instead of the hazard.
    """
    return {
        name
        for name in dir(store_class)
        if not name.startswith("_")
        and callable(getattr(store_class, name, None))
        and not inspect.iscoroutinefunction(getattr(store_class, name))
    }


def test_the_stores_live_where_the_guard_expects() -> None:
    """The guard overrides the methods the twins delegate to.

    If a store method is renamed, the guard silently stops covering it and
    these tests keep passing while the production path regresses. Assert the
    override set against the store's own public surface instead.
    """
    guarded = {
        name
        for name, value in vars(LoopGuardJobStore).items()
        if callable(value) and not name.startswith("_")
    }
    sync_surface = _sync_surface(JobStore)
    assert guarded == sync_surface, f"unguarded JobStore methods: {sync_surface - guarded}"

    guarded_result = {
        name
        for name, value in vars(LoopGuardResultStore).items()
        if callable(value) and not name.startswith("_")
    }
    result_surface = _sync_surface(CrawlResultStore)
    assert guarded_result == result_surface, (
        f"unguarded CrawlResultStore methods: {result_surface - guarded_result}"
    )
