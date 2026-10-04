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
import json
import inspect

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import job_runner
from app.crawl.job_store import TERMINAL_STATUSES, JobAccumulators, JobStatus, JobStore
from app.crawl.result_store import CrawlResultStore
from app.settings import CrawlerSettings
from tests._job_harnesses import (
    ScriptedExecute,
    _make_app,
    _seed_job,
    make_crawled_job_store,
    make_job_store,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier
from tests._result_builders import _canned_result

_ROOT = "http://example.test/a"


def _refuse_on_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(f"sync store method {name} ran on the event loop; the async twin owns that call")


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

    def status_row(self, job_id):
        _refuse_on_loop("JobStore.status_row")
        return super().status_row(job_id)

    def exists(self, job_id):
        _refuse_on_loop("JobStore.exists")
        return super().exists(job_id)

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
    return LoopGuardJobStore(tmp_path / "jobs.db", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())


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

    async def test_the_whole_job_endpoint_surface_stays_off_the_loop(self, jobs_client, loop_guard_store):
        # The crawl is held open across every assertion. Without it this test
        # raced its own background task: POST /v1/jobs starts the runner, the
        # fake engine returned instantly, and the job reached COMPLETED before
        # the cancel arrived -- so ``control`` answered 409 and the suite went
        # red at random under full-suite load while passing in isolation. A test
        # that only fails when the machine is busy is asserting on timing, not
        # on the off-the-loop behaviour it is named for.
        hang = asyncio.Event()
        job_runner.reset_job_runner()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(_ROOT)], hang=hang))
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

            controlled = await jobs_client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
            assert controlled.status_code == 200, controlled.text

            deleted = await jobs_client.delete(f"/v1/jobs/{job_id}")
            assert deleted.status_code == 200, deleted.text
            assert await _off_loop(loop_guard_store.load, job_id) is None
        finally:
            # Release the held crawl before the store fixture tears down, or the
            # background task outlives the loop and writes to a closed DB.
            hang.set()
            job_runner.reset_job_runner()


class TestStatusReadNeverMaterializesTheCrawl:
    """A status read must not read the pages it is only going to count.

    ``GET /v1/jobs/{id}`` returns counts -- frontier/visited/results sizes, a
    few scalars. The page history lives in two append-only tables, so those
    counts are an index probe. But ``load`` deserializes every result payload
    and every visited URL to build a ``CrawlJobState``, and the response then
    throws all of it away after calling ``len()`` on it. On a long job that is
    the entire crawl, read and JSON-decoded, per call, on the endpoint a client
    polls while its own crawl is running.

    The spy is a wrapper on the production accumulator methods, not a
    replacement: the real reads still execute, so a pass is evidence about the
    status path rather than about a double. It sits on the accumulator rather
    than on ``load`` because that also catches a status read that reaches for
    the history directly and skips ``load`` entirely.
    """

    _PAGES = 200

    @pytest.fixture
    def seeded_store(self, tmp_path):
        return make_crawled_job_store(tmp_path, self._PAGES)

    @staticmethod
    def _spy_on_history_reads(monkeypatch, store):
        reached: list[str] = []
        for name in ("read_results", "read_visited"):
            real = getattr(JobAccumulators, name)

            def wrapper(self, job_id, *, _name=name, _real=real):
                reached.append(_name)
                return _real(self, job_id)

            monkeypatch.setattr(JobAccumulators, name, wrapper)
        return reached

    def test_counts_come_without_reading_the_page_history(self, seeded_store, monkeypatch):
        store, job, reached = self._spying(seeded_store, monkeypatch)

        row = store.status_row(job.job_id)

        assert_no_history_read(reached, "the status read")
        assert row is not None
        assert row["visited_size"] == self._PAGES
        assert row["results_count"] == self._PAGES
        assert row["frontier_size"] == len(job.frontier)  # create seeds the root
        assert row["status"] == job.status.value
        assert row["stats"] == job.stats  # the row is the source of truth
        assert row["cap_truncated"] is False
        assert row["error"] is None

    def _spying(self, seeded_store, monkeypatch):
        """``(store, job, reached)`` with the history readers already instrumented.

        Every test in this class is "this read must not touch the history", so the
        instrumentation is part of the arrangement rather than of each test. Stated
        per test, the spy loop was the shared thing and the assertion was the
        variable one, which is backwards.
        """
        store, job = seeded_store
        return store, job, self._spy_on_history_reads(monkeypatch, store)

    def test_a_full_load_still_reads_the_history(self, seeded_store, monkeypatch):
        """The guard above is only meaningful if the full read still does.

        Without this the spy could be a no-op wrapper and the first test would
        pass for the wrong reason -- a spy that never fires proves nothing
        about the path it claims to watch.
        """
        store, job = seeded_store
        reached = self._spy_on_history_reads(monkeypatch, store)

        loaded = store.load(job.job_id)

        assert sorted(reached) == ["read_results", "read_visited"]
        assert len(loaded.visited) == self._PAGES

    def test_status_row_of_an_absent_job_is_none(self, tmp_path):
        store = make_job_store(tmp_path)
        assert store.status_row("no-such-job") is None

    def test_exists_probes_without_reading_the_row(self, seeded_store, monkeypatch):
        store, job, reached = self._spying(seeded_store, monkeypatch)

        assert store.exists(job.job_id) is True
        assert store.exists("no-such-job") is False
        assert_no_history_read(reached, "the existence probe")


def assert_no_history_read(reached, what: str) -> None:
    """`what` read none of the page history, and the message says which read.

    These are the tests that make the O(1) status projection worth having, and
    they all fail the same way: ``reached`` is not empty. A bare ``assert reached
    == []`` cannot say which of the three read paths regressed, and in a 2000-test
    run "reached" alone is not enough to find the test.
    """
    assert reached == [], f"{what} materialized the crawl: {reached}"


class TestStartupRecoveryStaysOffTheLoop:
    """Startup recovery runs inside the lifespan, so it has a loop to block.

    ``recover_orphaned_running_jobs`` rewound RUNNING rows and
    ``recover_and_start_pending_jobs`` listed every job and read each one back,
    all synchronously, on the loop that had just finished booting the service.
    """

    async def test_recovery_rewinds_and_starts_without_touching_the_loop(self, loop_guard_store):
        from app.api.job_runner import recover_and_start_pending_jobs

        job_runner.reset_job_runner()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(_ROOT)]))
        try:
            job = await _off_loop(_seed_job, loop_guard_store, _ROOT, {"limits": {"max_depth": 1}})
            await recover_and_start_pending_jobs(
                loop_guard_store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )
            status = await _await_terminal_status(loop_guard_store, job.job_id)
            assert status == "completed"
        finally:
            job_runner.reset_job_runner()

    async def test_rewinding_a_crash_orphan_never_touches_the_loop(self, loop_guard_store):
        from app.api.job_runner import recover_orphaned_running_jobs

        job = await _off_loop(_seed_job, loop_guard_store, _ROOT, {"limits": {"max_depth": 1}}, running=True)
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

        await _off_loop(loop_guard_store.create_single_shot, "req-1", _ROOT, {"strategies": []})
        # Completed, not fresh: this test is about *where* the read happens, and
        # a still-pending job would make it a test of the status contract as
        # well -- two questions, one assertion, and the second one was
        # answering the wrong way.
        await _off_loop(loop_guard_store.set_status, "req-1", JobStatus.COMPLETED, expected=JobStatus.PENDING)
        await _off_loop(result_store.put, "req-1", _stored_response("req-1"))

        response = await get_crawl_result("req-1", loop_guard_store, result_store)
        assert json.loads(response.body)["request_id"] == "req-1"

    async def test_a_pending_single_shot_read_stays_off_the_loop(self, loop_guard_store, result_store):
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(loop_guard_store.create_single_shot, "req-2", _ROOT, {"strategies": []})
        with pytest.raises(HTTPException) as raised:
            await get_crawl_result("req-2", loop_guard_store, result_store)
        assert raised.value.status_code == 202


def _stored_response(request_id: str) -> dict:
    """The payload the result store holds for one finished crawl.

    Three tests read a stored response and one writes one; each used to spell
    the five keys out, and a copy that dropped ``policy_schema_version`` asserts
    against a model that fills it in rather than against the stored shape.
    """
    return {
        "request_id": request_id,
        "results": [],
        "submitted_count": 0,
        "policy_schema_version": "",
        "success": True,
    }


async def _job_with_results_written(store, job_id: str) -> None:
    """Put a single-shot job into the state the runner passes through mid-flight.

    ``run_async_crawl`` persists ``job.results`` and only then flips the status
    to ``COMPLETED``, so this is a state a real job genuinely occupies, not a
    contrived one. Both halves of the status contract need it, and spelling it
    out twice is how the two tests would drift into testing different things.
    """
    await _off_loop(store.set_status, job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    job = await _off_loop(store.load, job_id)
    job.results = [_stored_response(job_id)]
    await _off_loop(store.save, job)


class TestStatusEndpointAnswersOnlyForTerminalJobs:
    """A 200 from ``GET /v1/crawl/{request_id}`` means the job is finished.

    An endpoint keyed on results alone answers 200 inside the gap between the
    runner's two writes, and a client that polls to 200 and then reads the job
    store finds a job still marked ``running`` -- told the crawl was done, and
    handed one that had not stopped.
    """

    async def test_a_running_job_that_has_written_results_is_still_pending(
        self, loop_guard_store, result_store
    ):
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(loop_guard_store.create_single_shot, "req-3", _ROOT, {"strategies": []})
        await _job_with_results_written(loop_guard_store, "req-3")

        with pytest.raises(HTTPException) as raised:
            await get_crawl_result("req-3", loop_guard_store, result_store)
        assert raised.value.status_code == 202, raised.value.detail

    async def test_a_stored_result_does_not_answer_200_for_a_running_job(
        self, loop_guard_store, result_store
    ):
        """The result store cannot be the thing that decides the answer.

        ``run_async_crawl`` publishes to the result store *after* its terminal
        status write, but that write is a compare-and-set whose result it does
        not check: a job paused between the two is published anyway. Keyed on
        the store alone -- which is what the endpoint's first branch does --
        that job answers 200 while the job store still says ``running``. That
        is the failure this class exists to prevent, reached by the other door.
        """
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(loop_guard_store.create_single_shot, "req-5", _ROOT, {"strategies": []})
        await _off_loop(loop_guard_store.set_status, "req-5", JobStatus.RUNNING, expected=JobStatus.PENDING)
        await _off_loop(result_store.put, "req-5", _stored_response("req-5"))

        with pytest.raises(HTTPException) as raised:
            await get_crawl_result("req-5", loop_guard_store, result_store)
        assert raised.value.status_code == 202, raised.value.detail

    async def test_a_stored_result_does_not_answer_200_for_a_paused_job(self, loop_guard_store, result_store):
        """The reachable form of the same hole: pause wins the race, publish loses it.

        ``pause_job`` compare-and-sets from ``RUNNING``, and ``run_async_crawl``
        compare-and-sets to ``COMPLETED`` from ``RUNNING``. Whichever lands
        first wins; when pause does, the runner's terminal write fails and the
        payload is still published. A paused job is stopped, not finished, so
        the answer is still 202.
        """
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(loop_guard_store.create_single_shot, "req-6", _ROOT, {"strategies": []})
        await _off_loop(loop_guard_store.set_status, "req-6", JobStatus.RUNNING, expected=JobStatus.PENDING)
        await _off_loop(loop_guard_store.set_status, "req-6", JobStatus.PAUSED, expected=JobStatus.RUNNING)
        await _off_loop(result_store.put, "req-6", _stored_response("req-6"))

        with pytest.raises(HTTPException) as raised:
            await get_crawl_result("req-6", loop_guard_store, result_store)
        assert raised.value.status_code == 202, raised.value.detail

    async def test_a_completed_job_with_a_stored_result_still_answers_200(
        self, loop_guard_store, result_store
    ):
        """The complement on the store path: the fix must not make it unreadable."""
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(loop_guard_store.create_single_shot, "req-7", _ROOT, {"strategies": []})
        await _off_loop(loop_guard_store.set_status, "req-7", JobStatus.RUNNING, expected=JobStatus.PENDING)
        await _off_loop(loop_guard_store.set_status, "req-7", JobStatus.COMPLETED, expected=JobStatus.RUNNING)
        await _off_loop(result_store.put, "req-7", _stored_response("req-7"))

        response = await get_crawl_result("req-7", loop_guard_store, result_store)
        assert json.loads(response.body)["request_id"] == "req-7"

    async def test_the_same_job_answers_200_once_it_is_completed(self, loop_guard_store, result_store):
        """The complement: the fix must not make a finished job unreadable."""
        from app.api.crawl_endpoint import get_crawl_result

        await _off_loop(loop_guard_store.create_single_shot, "req-4", _ROOT, {"strategies": []})
        await _job_with_results_written(loop_guard_store, "req-4")
        await _off_loop(loop_guard_store.set_status, "req-4", JobStatus.COMPLETED, expected=JobStatus.RUNNING)

        response = await get_crawl_result("req-4", loop_guard_store, result_store)
        assert json.loads(response.body)["request_id"] == "req-4"


async def _await_terminal_status(store: JobStore, job_id: str) -> str:
    """Poll the status off-loop until it is terminal.

    The store forbids on-loop reads, so the harness cannot use the edge-driven
    ``_wait_status`` (it reads on the loop). The bound here is the same hang
    guard the job harness documents; a job that is progressing reaches a
    terminal status on its own, and one that is not should fail loudly.
    Callers assert which terminal status they wanted -- the set is only what
    ends the wait, so it is read from the enum rather than restated here.
    """
    for _ in range(int(30.0 / 0.005)):
        job = await _off_loop(store.load, job_id)
        if job is not None and job.status in TERMINAL_STATUSES:
            return job.status.value
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never reached a terminal status")


def test_the_guard_itself_bites():
    """A guard that cannot fail proves nothing, so prove it fires.

    ``asyncio.run`` gives a real running loop on this thread, which is the
    condition the guard exists to catch.
    """

    async def call_the_sync_surface() -> None:
        store = LoopGuardJobStore(":memory:", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
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
