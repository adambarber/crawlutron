"""A lost claim must stop the job at each early-return site."""

from __future__ import annotations

import asyncio

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.json_value import JsonValue
from crawlutron.service import job_runner
from crawlutron.storage.job_store import FrontierEntry, JobStatus, JobStore
from crawlutron.storage.safe_db import allow_on_loop
from tests._job_store_doubles import make_job_store
from tests._policy_factories import canonical

ROOT = "http://example.test/root"
POLICY: dict[str, JsonValue] = {"limits": {"max_depth": 0, "max_pages": 1, "respect_robots": False}}
JOB_ID = "job-lost-claim"


def _store(tmp_path) -> JobStore:
    return make_job_store(tmp_path)


class _LostClaimStore(JobStore):
    """A JobStore whose ``renew_job_claim`` loses ownership on a chosen call."""

    def __init__(self, store: JobStore, lose_on_call: int) -> None:
        self._store = store
        self._lose_on_call = lose_on_call
        self._calls = 0

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def renew_job_claim(self, job_id: str, token: str, *, now: float) -> bool:
        self._calls += 1
        if self._calls == self._lose_on_call:
            return False
        with allow_on_loop():
            return self._store.renew_job_claim(job_id, token, now=now)

    async def arenew_job_claim(self, job_id: str, token: str, *, now: float) -> bool:
        self._calls += 1
        if self._calls == self._lose_on_call:
            return False
        with allow_on_loop():
            return self._store.renew_job_claim(job_id, token, now=now)


async def _fake_execute(payload, *, settings, shutdown_event):
    return {
        "request_id": "test",
        "results": [],
        "submitted_count": 0,
        "policy_schema_version": "v1",
    }


async def _run_job(store: JobStore) -> None:
    job_runner.install_execute_crawl(_fake_execute)
    try:
        await job_runner.run_job(JOB_ID, store, CrawlerSettings.from_mapping({}), asyncio.Event())
    finally:
        job_runner.reset_execute_crawl()


async def test_lost_claim_stops_at_loop_level_renewal(tmp_path):
    """Renewal failure at the loop-level ``_renew_claim_and_save`` stops the job."""
    store = _LostClaimStore(_store(tmp_path), lose_on_call=2)
    with allow_on_loop():
        store.create_single_shot(JOB_ID, ROOT, POLICY, frontier=[FrontierEntry(url=canonical(ROOT), depth=0)])

    await _run_job(store)

    reloaded = await store.aload(JOB_ID)
    assert reloaded is not None
    assert reloaded.status is JobStatus.RUNNING
    store.close()


async def test_lost_claim_stops_at_batch_level_renewal(tmp_path):
    """Renewal failure at the batch-level ``_renew_claim_and_save`` stops the job."""
    store = _LostClaimStore(_store(tmp_path), lose_on_call=4)
    with allow_on_loop():
        store.create_single_shot(JOB_ID, ROOT, POLICY, frontier=[FrontierEntry(url=canonical(ROOT), depth=0)])

    await _run_job(store)

    reloaded = await store.aload(JOB_ID)
    assert reloaded is not None
    assert reloaded.status is JobStatus.RUNNING
    store.close()
