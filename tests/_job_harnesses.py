"""Job-runner test harness: stores, fake engine seams, and the job-state waits.

The waits are EDGE-triggered, not a poll racing a deadline: ``set_status`` is
the complete edge set, so the wait returns the instant the status is committed."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crawlutron.admission.policy import PolicyLike
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.json_value import JsonValue
from crawlutron.models.taxonomy import Classification
from crawlutron.storage.job_store import TERMINAL_STATUSES, JobStatus, JobStore
from tests._app_harnesses import harness_app
from tests._dependency_overrides import bind_base_overrides

if TYPE_CHECKING:
    from crawlutron.models.normalized_url import NormalizedUrl
    from crawlutron.models.result_record import ProducerEnvelope

from tests._job_drivers import (
    _create_api_job,
    _seed_job,
    assert_settled,
    drive_job,
    job_limits,
    post_api_job,
    post_render_with_pool,
    raw_job_row,
    recording_execute,
    run_api_job,
    run_scripted_api_job,
    run_seeded_job,
    run_seeded_scripted_job,
    run_to_terminal,
    submit_blocked_job,
    write_row_policy,
)
from tests._job_store_doubles import (
    _StoreListener,
    frontier,
    loaded_job,
    make_crawled_job_store,
    make_job_store,
    seed_frontier,
    single_shot_job,
    single_shot_orphan,
)
from tests._policy_factories import canonical
from tests._result_builders import engine_result

#: Hang guard for the store waits. Deliberately NOT ``HANG_GUARD_SECONDS``: that one guards waits for a task
#: the SAME loop has already scheduled, and such a task either runs on the next turn or never.
STORE_HANG_GUARD_SECONDS = 30.0


#: The statuses a job settles in, as the store spells them.
_SETTLED_STATUSES = frozenset(status.value for status in TERMINAL_STATUSES)


_JOINABLE_STATUSES = _SETTLED_STATUSES - {JobStatus.CANCELLED.value}


async def run_job_control_test(
    app: FastAPI,
    job_store: JobStore,
    job_id: str,
    action: str,
    expected_status: int = 422,
) -> None:

    async with ASGITransport(app=app) as transport:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": action})
            assert resp.status_code == expected_status


async def _execution_urls(payload, hang: asyncio.Event | None):
    if hang is not None and not hang.is_set():
        await hang.wait()
    if hasattr(payload, "urls") and hasattr(payload, "url"):
        urls = payload.urls if payload.urls is not None else ([payload.url] if payload.url else [])
    else:
        urls = payload.get("urls") or [payload.get("url")]
    return [canonical(url) for url in urls]


def _execution_payload(results: list[EngineCrawlResult], submitted_count: int) -> ProducerEnvelope:
    return {
        "request_id": "test",
        "results": results,
        "submitted_count": submitted_count,
        "policy_schema_version": "test",
    }


def scripted_engine(monkeypatch, target, scripted: ScriptedExecute) -> None:
    """Swap ``target``'s execute_crawl for a scripted stand-in: the shared
    engine seam both delta door tests patch (HTTP door and MCP tool)."""

    async def _execute(payload_dict, *, settings, shutdown_event, url_pairs=None, spill_store=None):
        return await scripted(payload_dict, settings=settings, shutdown_event=shutdown_event)

    monkeypatch.setattr(target, "execute_crawl", _execute)


class ScriptedExecute:
    """The engine seam, faked, owning the per-URL attempt count.

    The count belongs to the fetcher, not the caller (four wrappers collapsed
    into one). ``scripts``: per-URL sequences; unscripted URLs succeed."""

    def __init__(
        self,
        scripts: dict[str, list[EngineCrawlResult]] | None = None,
        *,
        results: list[EngineCrawlResult] | None = None,
        hang: asyncio.Event | None = None,
    ):
        if (scripts is None) == (results is None):
            raise AssertionError("ScriptedExecute takes exactly one of scripts= or results=")
        # Keys are URL identities: caller-spelled text is admitted here (so a key like "http://example.test"
        if scripts is not None:
            self._scripts = {canonical(url).raw: list(seq) for url, seq in scripts.items()}
        else:
            self._scripts = {r.original_url: [r] for r in results or []}
        self._default: Classification = (
            Classification.rendered if scripts is not None else Classification.site_error
        )
        self._hang = hang
        self.attempts: dict[str, int] = {}

    def _entry(self, url: NormalizedUrl) -> EngineCrawlResult:
        sequence = self._scripts.get(url.raw)
        if sequence is None:
            return engine_result(url, self._default)
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    async def __call__(self, payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, self._hang)
        for url in urls:
            self.attempts[url.raw] = self.attempts.get(url.raw, 0) + 1
        return _execution_payload([self._entry(url) for url in urls], len(urls))


def _make_app(job_store):
    from crawlutron.api.jobs_router import router as jobs_router

    app = harness_app(jobs_router)
    shutdown = asyncio.Event()
    bind_base_overrides(app, job_store=job_store, shutdown_event=shutdown)
    return app


async def _join_runner_task(job_id: str) -> bool:
    """Await the runner task for ``job_id`` if one is registered and live.

    For a terminal wait this is the exact event the caller waits on, with no
    deadline to race; ``asyncio.wait`` resolves however the task finished."""
    from crawlutron.service import job_lifecycle

    task = job_lifecycle.tracked_task(job_id)
    if task is None or task.done():
        return False
    await asyncio.wait({task})
    return True


async def _next_store_edge(store: JobStore, job_id: str, listener: _StoreListener) -> None:
    """Block until the next write to ``job_id``, or fail naming what never came.

    A HANG GUARD, not an instrument: edge-triggered, so a slow job takes longer
    instead of failing; fires only when NOTHING is written (measured, 30s)."""
    listener.event.clear()
    try:
        await asyncio.wait_for(listener.event.wait(), STORE_HANG_GUARD_SECONDS)
    except TimeoutError as exc:
        raise AssertionError(
            f"no write reached job {job_id} for {STORE_HANG_GUARD_SECONDS}s. The wait is "
            "edge-triggered, so nothing elapsed: the store was not written to at all, "
            f"which is a wedge rather than slow progress. Last state: {store.load(job_id)}"
        ) from exc


async def _wait_for(job_store, job_id: str, satisfied, *, join_task: bool):
    """Block until ``satisfied(job)`` holds; return that job.

    Subscribe, then read, then wait -- the state can already be what the caller
    asked for; clearing after the read keeps the wait lossless."""
    bus = getattr(job_store, "status_bus", None)
    if bus is None:
        raise AssertionError(
            "this job store has no status bus: build it with make_job_store() so "
            "job-state waits are edge-triggered instead of polling a deadline"
        )
    listener = bus.subscribe(job_id)
    try:
        while True:
            job = job_store.load(job_id)
            if job is not None and satisfied(job):
                return job
            if join_task and await _join_runner_task(job_id):
                continue  # the row may have moved; re-read before waiting again
            await _next_store_edge(job_store, job_id, listener)
    finally:
        listener.close()


async def _wait_status(job_store, job_id: str, statuses: Collection[str]) -> str:
    """Block until ``job_id`` holds one of ``statuses``; return the status name.

    A terminal wait joins the live runner task (no deadline at all); an
    intermediate-state predicate falls back to the hang guard alone."""
    job = await _wait_for(
        job_store,
        job_id,
        lambda row: row.status.value in statuses,
        join_task=bool(statuses) and frozenset(statuses) <= _JOINABLE_STATUSES,
    )
    return job.status.value


async def _wait_until(job_store, job_id: str, predicate):
    """Block until ``predicate(job)`` holds; return that job.

    The predicate ranges over the whole row (frontier length, stat counters), so
    ``save`` is the edge source rather than the status CAS alone.
    """
    return await _wait_for(job_store, job_id, predicate, join_task=False)


def _execution_policy(policy: PolicyLike | None) -> PolicyLike:
    effective = dict(policy or {})
    limits_raw = effective.get("limits")
    limits: dict[str, JsonValue] = limits_raw if isinstance(limits_raw, dict) else {}
    limits.setdefault("max_depth", 1)
    limits.setdefault("max_pages", 0)
    limits.setdefault("respect_robots", False)
    effective["limits"] = limits
    discovery_raw = effective.get("discovery")
    discovery: dict[str, JsonValue] = discovery_raw if isinstance(discovery_raw, dict) else {}
    discovery.setdefault("sitemaps", False)
    effective["discovery"] = discovery
    return effective


__all__ = [
    "ScriptedExecute",
    "scripted_engine",
    "_create_api_job",
    "_execution_payload",
    "_execution_policy",
    "_execution_urls",
    "_make_app",
    "_seed_job",
    "_wait_status",
    "_wait_until",
    "assert_settled",
    "drive_job",
    "frontier",
    "job_limits",
    "loaded_job",
    "make_crawled_job_store",
    "make_job_store",
    "post_api_job",
    "post_render_with_pool",
    "raw_job_row",
    "recording_execute",
    "run_api_job",
    "run_job_control_test",
    "run_scripted_api_job",
    "run_seeded_job",
    "run_seeded_scripted_job",
    "run_to_terminal",
    "seed_frontier",
    "single_shot_job",
    "single_shot_orphan",
    "submit_blocked_job",
    "write_row_policy",
]
