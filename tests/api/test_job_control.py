"""Job control, status shape and visited envelopes stay consistent."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from crawlutron.api import job_runner
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import FrontierEntry, JobStatus, JobStore
from tests._job_harnesses import (
    ScriptedExecute,
    _create_api_job,
    _wait_status,
    _wait_until,
    drive_job,
    post_api_job,
    run_api_job,
    run_job_control_test,
    run_scripted_api_job,
    submit_blocked_job,
)
from tests._policy_factories import canonical
from tests._result_builders import (
    GRAPH_CHILD,
    GRAPH_ROOT,
    _canned_result,
    _root_child_results,
    _root_fans_out,
)
from tests._timing_harnesses import await_until


class TestStatusResponseShape:
    """The job status response is a projection of the status view, not a subset.

    The persisted ``kind`` was splatted into an ``extra="ignore"`` model and
    dropped without a word; asserted here through the HTTP surface."""

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

        A hard-coded one would satisfy the whole-site case while lying to every
        single-shot caller -- the half that decides resumption."""
        job_store.create_single_shot("shot-1", GRAPH_ROOT, CrawlPolicy())

        response = await client.get("/v1/jobs/shot-1")

        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "single_shot"


class TestJobStateWaitContract:
    """The wait itself is the contract, so it is pinned like one.

    ``_wait_status`` polled against a 5.0s deadline -- a starvation escape hatch
    wearing a deadline's clothes; these tests hold the line."""

    async def test_wait_status_rides_the_write_not_the_clock(self, job_store, monkeypatch):
        """The wait blocks until the control-plane write lands, then returns it.

        A waiter created before the claim can not have completed -- nothing for
        it to have seen -- so "still blocked" is an observation, not a bet."""
        gate = threading.Event()
        real_set_status = job_store.set_status

        def gated_set_status(job_id, new, *, expected, error=None):
            if new is JobStatus.RUNNING:
                gate.wait(timeout=10)
            return real_set_status(job_id, new, expected=expected, error=error)

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

        The "workable" fallback -- poll until the state matches -- is exactly
        the implementation that raced a starved box, so the harness refuses."""
        from crawlutron.storage.job_store import JobStore
        from tests._job_harnesses import _wait_status as wait_status
        from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier

        bare = JobStore(tmp_path / "bare.db", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
        assert not hasattr(bare, "status_bus")
        job_id = bare.create(GRAPH_ROOT, CrawlPolicy()).job_id
        with pytest.raises(AssertionError, match="no status bus"):
            await wait_status(bare, job_id, {"completed"})


class TestJobControlOverRunner:
    """Control transitions observed on a live runner, not on a finished row.

    Each flips a control bit mid-batch, so the engine is held and released at
    the moment the test means it -- see :func:`submit_blocked_job`."""

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
    """A control action a job is not in a state to accept is a state conflict.

    The three cases differ only in action and seed state, so the wiring is one
    helper; a copy let the seed-to-terminal line be forgotten."""

    @staticmethod
    async def _control(action: str, expected: int, *, seed_terminal: bool = False) -> None:
        from crawlutron.main import job_store
        from crawlutron.storage.job_store import JobStatus
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

    A URL in ``visited`` without a result read as ``total_crawled: 2`` with a
    single result; this asserts the runner cannot drift from the batch path."""

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

        The claim is the flip the two paths below exercise; the state setup
        does not differ between them."""
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

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def aload(self, job_id: str):
        job = await self._inner.aload(job_id)
        return replace(job, status=JobStatus.RUNNING) if job is not None else None
