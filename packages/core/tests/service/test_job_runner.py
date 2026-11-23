"""#1155: a transient storage failure pauses a running job for recovery instead of failing it terminally."""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.json_value import JsonValue
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.service import job_runner
from crawlutron.storage.job_store import DEFAULT_JOB_CLAIM_SECONDS, JobStatus
from crawlutron.storage.safe_db import (
    StorageBusy,
    StorageError,
    StoreConnectionPoisoned,
    WriteRejected,
)
from tests._job_drivers import _seed_job
from tests._job_harnesses import make_job_store

ROOT = "http://example.test/root"
POLICY: dict[str, JsonValue] = {"limits": {"max_depth": 0, "max_pages": 1, "respect_robots": False}}

#: The fixed retry bound in crawlutron.service.job_runner; the count assert
#: below pins the observed number against it (the mutation proof discriminates it).
_RETRY_ATTEMPTS = 3


class _FailingEngine:
    """Engine stand-in that fails every batch with one storage error and counts calls."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def __call__(
        self, payload: object, *, settings: object, shutdown_event: object
    ) -> ProducerEnvelope:
        self.calls += 1
        raise self.error


class TestStorageFailurePausesForRecovery:
    async def test_storage_busy_in_the_claimed_loop_ends_paused_not_failed(self, tmp_path) -> None:
        """StorageError in _run_claimed_loop exhausts retries and ends PAUSED, never FAILED."""
        store = make_job_store(tmp_path)
        job = _seed_job(store, ROOT, POLICY, frontier=[ROOT])
        engine = _FailingEngine(StorageBusy("database is locked"))
        job_runner.install_execute_crawl(engine)
        try:
            await job_runner.run_job(job.job_id, store, CrawlerSettings.from_mapping({}), asyncio.Event())
        finally:
            job_runner.reset_execute_crawl()
        finished = await store.aload(job.job_id)
        assert finished is not None
        assert finished.status is JobStatus.PAUSED
        assert finished.status is not JobStatus.FAILED

    async def test_non_storage_exception_still_fails_terminal(self, tmp_path) -> None:
        """A non-storage exception keeps the pre-existing terminal-failure path."""
        store = make_job_store(tmp_path)
        job = _seed_job(store, ROOT, POLICY, frontier=[ROOT])
        job_runner.install_execute_crawl(_FailingEngine(ValueError("boom")))
        try:
            await job_runner.run_job(job.job_id, store, CrawlerSettings.from_mapping({}), asyncio.Event())
        finally:
            job_runner.reset_execute_crawl()
        finished = await store.aload(job.job_id)
        assert finished is not None
        assert finished.status is JobStatus.FAILED

    async def test_pause_transition_raising_is_fail_safe_not_failed(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If aset_status(PAUSED) itself raises StorageError the row stays RUNNING for recovery."""
        store = make_job_store(tmp_path)
        job = _seed_job(store, ROOT, POLICY, frontier=[ROOT])
        job_runner.install_execute_crawl(_FailingEngine(StorageBusy("database is locked")))
        real_aset_status = store.aset_status

        async def _failing_pause(
            job_id: str,
            new: JobStatus,
            *,
            expected: JobStatus | frozenset[JobStatus],
            error: str | None = None,
            claimed_by: str | None = None,
            claim_now: float | None = None,
            clear_claim: bool = False,
            claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
        ) -> bool:
            if new is JobStatus.PAUSED:
                raise StorageBusy("pause write locked")
            return await real_aset_status(
                job_id,
                new,
                expected=expected,
                error=error,
                claimed_by=claimed_by,
                claim_now=claim_now,
                clear_claim=clear_claim,
                claim_seconds=claim_seconds,
            )

        monkeypatch.setattr(store, "aset_status", _failing_pause)
        try:
            await job_runner.run_job(job.job_id, store, CrawlerSettings.from_mapping({}), asyncio.Event())
        finally:
            job_runner.reset_execute_crawl()
        finished = await store.aload(job.job_id)
        assert finished is not None
        assert finished.status is JobStatus.RUNNING
        assert finished.status is not JobStatus.FAILED
        assert finished.claimed_by is None  # cleared so boot recovery rewinds the row

    @pytest.mark.parametrize(
        "error",
        [StorageBusy("locked"), StoreConnectionPoisoned("poisoned"), WriteRejected("rejected")],
    )
    async def test_every_storage_error_subclass_pauses_after_the_fixed_bound(
        self, tmp_path, error: StorageError
    ) -> None:
        """StorageBusy, StoreConnectionPoisoned and WriteRejected all exhaust the fixed bound then pause."""
        store = make_job_store(tmp_path)
        job = _seed_job(store, ROOT, POLICY, frontier=[ROOT])
        engine = _FailingEngine(error)
        job_runner.install_execute_crawl(engine)
        try:
            await job_runner.run_job(job.job_id, store, CrawlerSettings.from_mapping({}), asyncio.Event())
        finally:
            job_runner.reset_execute_crawl()
        finished = await store.aload(job.job_id)
        assert finished is not None
        assert finished.status is JobStatus.PAUSED
        assert engine.calls == _RETRY_ATTEMPTS
