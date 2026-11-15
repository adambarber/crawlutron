"""#987: RUNNING jobs carry a DB lease so recovery never rewinds a live owner."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.json_value import JsonValue
from crawlutron.service import job_recovery, job_runner
from crawlutron.storage.job_store import JobStatus, JobStore
from crawlutron.storage.safe_db import allow_on_loop
from tests._job_drivers import _seed_job
from tests._job_harnesses import ScriptedExecute, _wait_status, _wait_until
from tests._result_builders import engine_result

ROOT = "http://example.test/root"
CHILD = "http://example.test/child"
POLICY: dict[str, JsonValue] = {"limits": {"max_depth": 0, "max_pages": 0, "respect_robots": False}}


def _store(tmp_path) -> JobStore:
    from tests._job_harnesses import make_job_store

    return make_job_store(tmp_path)


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _seed_running(store: JobStore, root: str = ROOT, frontier: list[str] | None = None):
    return _seed_job(store, root, POLICY, frontier=frontier or [root], running=True)


def _seed_foreign(store: JobStore, owner: str = "worker-foreign", now: float | None = None):
    job = _seed_running(store)
    at = time.time() if now is None else now
    with allow_on_loop():
        assert store.claim_running(job.job_id, owner, now=at, claim_seconds=300.0)
    return store, job


@pytest.mark.wall_assertion
class TestRecoveryRespectsJobLease:
    async def test_recovery_leaves_fresh_foreign_claimed_running_job(self, tmp_path):
        """Recovery must not rewind a RUNNING row owned by a live foreign lease."""
        store, job = _seed_foreign(_store(tmp_path))
        assert await job_recovery.recover_orphaned_running_jobs(store) == 0
        reloaded = store.load(job.job_id)
        assert reloaded and reloaded.status is JobStatus.RUNNING and reloaded.claimed_by == "worker-foreign"

    async def test_recovery_retires_unclaimed_running_orphan(self, tmp_path):
        """An unclaimed RUNNING orphan is still a crash orphan recovery must rewind."""
        store = _store(tmp_path)
        job = _seed_running(store)
        assert await job_recovery.recover_orphaned_running_jobs(store) == 1
        reloaded = store.load(job.job_id)
        assert reloaded and reloaded.status is JobStatus.PENDING and reloaded.claimed_by is None

    async def test_recovery_retires_stale_claimed_running_orphan(self, tmp_path):
        """A stale lease is abandoned ownership: recovery may retire the row."""
        store = _store(tmp_path)
        job = _seed_running(store)
        with allow_on_loop():
            assert store.claim_running(job.job_id, "worker-dead", now=100.0, claim_seconds=300.0)
        assert await job_recovery.recover_orphaned_running_jobs(store, now=500.0) == 1
        reloaded = store.load(job.job_id)
        assert reloaded is not None and reloaded.status is JobStatus.PENDING

    async def test_stale_lease_boundary_is_exclusive(self, tmp_path):
        """A claim is fresh at the window edge and stale only after it."""
        store = _store(tmp_path)
        fresh, stale = _seed_running(store, ROOT), _seed_running(store, CHILD, [CHILD])
        with allow_on_loop():
            for job, owner in ((fresh, "worker-a"), (stale, "worker-b")):
                assert store.claim_running(job.job_id, owner, now=100.0, claim_seconds=300.0)
        assert await job_recovery.recover_orphaned_running_jobs(store, now=400.0) == 0
        fresh_job = store.load(fresh.job_id)
        assert fresh_job is not None and fresh_job.status is JobStatus.RUNNING
        assert await job_recovery.recover_orphaned_running_jobs(store, now=400.001) == 2
        fresh_job = store.load(fresh.job_id)
        stale_job = store.load(stale.job_id)
        assert fresh_job is not None and fresh_job.status is JobStatus.PENDING
        assert stale_job is not None and stale_job.status is JobStatus.PENDING

    async def test_recovery_cannot_retire_row_claimed_fresh_during_scan(self, tmp_path):
        """Retire CAS must lose if the row becomes freshly claimed."""
        store = _store(tmp_path)
        job = _seed_running(store)
        with allow_on_loop():
            assert store.retire_orphan_running(job.job_id, JobStatus.PENDING, now=100.0) is True
            store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
            assert store.claim_running(job.job_id, "worker-live", now=200.0, claim_seconds=300.0)
            assert store.retire_orphan_running(job.job_id, JobStatus.PENDING, now=250.0) is False
        reloaded = store.load(job.job_id)
        assert reloaded is not None and reloaded.status is JobStatus.RUNNING


@pytest.mark.wall_assertion
class TestRunnerClaimOwnership:
    async def _seed_and_start_hung(self, store):
        job = _seed_job(store, ROOT, POLICY, frontier=[ROOT])
        hang = asyncio.Event()
        results = [engine_result(ROOT, "rendered")]
        job_runner.install_execute_crawl(ScriptedExecute(results=results, hang=hang))
        settings = CrawlerSettings.from_mapping({})
        task = job_runner.start_job(job.job_id, store, settings, asyncio.Event())
        await _wait_status(store, job.job_id, {"running"})
        return job, hang, task

    async def test_runner_stamps_claim_on_start(self, tmp_path):
        """Starting a job claims the RUNNING row for the runner."""
        store = _store(tmp_path)
        job, hang, task = await self._seed_and_start_hung(store)
        try:
            claim = store.running_claim(job.job_id)
            assert claim is not None
            assert claim[0] and claim[1] is not None and claim[2] == JobStatus.RUNNING.value
        finally:
            hang.set()
            await asyncio.wait({task})

    async def test_runner_stops_after_losing_ownership_and_cannot_complete(self, tmp_path):
        """A runner that loses its lease must not finish the row it no longer owns."""
        store = _store(tmp_path)
        job, hang, task = await self._seed_and_start_hung(store)
        try:
            await _wait_until(store, job.job_id, lambda j: bool(j.in_flight))
            current = store.load(job.job_id)
            assert current is not None and current.claimed_by is not None
            with allow_on_loop():
                assert store.release_job_claim(job.job_id, current.claimed_by)
                assert store.claim_running(job.job_id, "worker-foreign", now=time.time(), claim_seconds=300.0)
            hang.set()
            await asyncio.wait({task})
            reloaded = store.load(job.job_id)
            assert reloaded is not None and reloaded.status is JobStatus.RUNNING
            assert reloaded.claimed_by == "worker-foreign"
        finally:
            hang.set()
            await _cancel_task(task)

    async def test_runner_cannot_record_terminal_failure_after_losing_ownership(self, tmp_path):
        """arecord_terminal_failure must not overwrite a row owned by someone else."""
        store, job = _seed_foreign(_store(tmp_path))
        recorded = await job_runner.arecord_terminal_failure(
            store, job.job_id, "boom", claimed_by="worker-stale"
        )
        assert recorded is False
        reloaded = store.load(job.job_id)
        assert reloaded and reloaded.status is JobStatus.RUNNING and reloaded.claimed_by == "worker-foreign"
        assert reloaded.error is None
        store.close()

    async def test_owned_terminal_failure_clears_runner_lease(self, tmp_path):
        """Owned terminal failure must release the runner claim on transition."""
        store = _store(tmp_path)
        job = _seed_running(store)
        with allow_on_loop():
            assert store.claim_running(job.job_id, "worker-owner", now=time.time(), claim_seconds=300.0)
        recorded = await job_runner.arecord_terminal_failure(
            store, job.job_id, "boom", claimed_by="worker-owner"
        )
        assert recorded is True
        reloaded = store.load(job.job_id)
        assert reloaded is not None and reloaded.status is JobStatus.FAILED
        assert reloaded.error == "boom"
        assert reloaded.claimed_by is None and reloaded.claimed_at is None
        store.close()


@pytest.mark.wall_assertion
class TestClaimRenewalAndRetirementEdges:
    def test_renew_job_claim_requires_live_owner(self, tmp_path):
        store = _store(tmp_path)
        job = _seed_running(store)
        now = time.time()
        with allow_on_loop():
            assert store.claim_running(job.job_id, "worker-owner", now=now, claim_seconds=300.0)
            assert store.renew_job_claim(job.job_id, "worker-owner", now=now + 1.0) is True
            assert store.renew_job_claim(job.job_id, "worker-other", now=now + 2.0) is False
            assert store.release_job_claim(job.job_id, "worker-owner") is True
            assert store.renew_job_claim(job.job_id, "worker-owner", now=now + 3.0) is False

    async def test_retire_orphan_running_requires_abandoned_lease(self, tmp_path):
        store = _store(tmp_path)
        job = _seed_running(store)
        now = 1000.0
        with allow_on_loop():
            assert store.claim_running(job.job_id, "worker-live", now=now, claim_seconds=300.0)
        assert await job_recovery.recover_orphaned_running_jobs(store, now=1200.0, claim_seconds=300.0) == 0
        assert await job_recovery.recover_orphaned_running_jobs(store, now=1400.1, claim_seconds=300.0) == 1
        reloaded = store.load(job.job_id)
        assert reloaded is not None and reloaded.status is JobStatus.PENDING
        assert reloaded.claimed_by is None and reloaded.claimed_at is None
        store.close()
