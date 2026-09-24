import asyncio
import pathlib
from typing import Any

from _result_builders import _canned_result

from app.crawl.job_store import JobStatus, JobStore
from app.validation import HostPolicy

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()


def make_job_store(tmpdir: str) -> JobStore:
    db_path = pathlib.Path(tmpdir) / "jobs.db"
    return JobStore(db_path, policy=UNRESTRICTED_HOST_POLICY)


async def run_job_control_test(
    app,
    job_store: JobStore,
    job_id: str,
    action: str,
    expected_status: int = 422,
) -> None:
    from httpx import ASGITransport, AsyncClient

    async with ASGITransport(app=app) as transport:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": action})
            assert resp.status_code == expected_status


async def _execution_urls(payload_dict, hang: asyncio.Event | None):
    if hang is not None and not hang.is_set():
        await hang.wait()
    return payload_dict.get("urls") or [payload_dict.get("url")]


def _execution_payload(results: list[dict], submitted_count: int) -> dict:
    return {
        "results": results,
        "submitted_count": submitted_count,
        "policy_schema_version": "test",
    }


def _fake_execute_scripted(
    scripts: dict[str, list[dict]],
    *,
    attempts: dict[str, int] | None = None,
    hang: asyncio.Event | None = None,
):
    async def _execute(payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, hang)
        results = []
        for url in urls:
            sequence = scripts.get(url)
            if sequence is None:
                entry = _canned_result(url)
            elif len(sequence) > 1:
                entry = sequence.pop(0)
            else:
                entry = sequence[0]
            if attempts is not None:
                attempts[url] = attempts.get(url, 0) + 1
            results.append(entry)
        return _execution_payload(results, len(urls))

    return _execute


def _fake_execute(results: list[dict], *, hang: asyncio.Event | None = None):
    async def _execute(payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, hang)
        by_url = {result["original_url"]: result for result in results}
        selected = [by_url.get(url, _canned_result(url, status="failed")) for url in urls]
        return _execution_payload(selected, len(urls))

    return _execute


def _make_app(job_store):
    from fastapi import FastAPI

    from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
    from app.api.jobs_router import router as jobs_router
    from app.settings import CrawlerSettings

    app = FastAPI()
    app.include_router(jobs_router)
    shutdown = asyncio.Event()
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: shutdown
    return app


async def _wait_status(job_store, job_id: str, statuses: set[str], timeout: float = 5.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = job_store.load(job_id)
        if job is not None and job.status.value in statuses:
            return job.status.value
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {statuses}; last={job_store.load(job_id)}")


async def _wait_until(job_store, job_id: str, predicate, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = job_store.load(job_id)
        if job is not None and predicate(job):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"predicate never satisfied; last={job_store.load(job_id)}")


def _execution_policy(policy: dict | None) -> dict:
    effective = dict(policy or {})
    limits = dict(effective.get("limits") or {})
    limits.setdefault("max_depth", 1)
    limits.setdefault("max_pages", 0)
    limits.setdefault("respect_robots", False)
    effective["limits"] = limits
    discovery = dict(effective.get("discovery") or {})
    discovery.setdefault("sitemaps", False)
    effective["discovery"] = discovery
    return effective


async def _create_api_job(client, root_url: str, execute, policy: dict | None = None):
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    response = await client.post(
        "/v1/jobs",
        json={"root_url": root_url, "policy": _execution_policy(policy)},
    )
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _seed_job(
    job_store,
    root_url: str,
    policy: dict | None = None,
    *,
    frontier: list[str] | None = None,
    in_flight: list[str] | None = None,
    running: bool = False,
):
    job = job_store.create(root_url, policy if policy is not None else {})
    if frontier is not None:
        job.frontier = list(frontier)
    if in_flight is not None:
        job.in_flight = list(in_flight)
    job_store.save(job)
    if running:
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    return job


async def _run_persisted_job(job, job_store, execute):
    from app.api import job_runner
    from app.settings import CrawlerSettings

    job_runner.install_execute_crawl(execute)
    await job_runner._run_job(
        job.job_id,
        job_store,
        CrawlerSettings.from_mapping({}),
        asyncio.Event(),
    )
    status = await _wait_status(job_store, job.job_id, {"completed", "failed"})
    assert status in {"completed", "failed"}
    finished = job_store.load(job.job_id)
    assert finished is not None
    return finished


async def _run_scripted_persisted_job(job, job_store, result_map):
    attempts: dict[str, int] = {}
    finished = await _run_persisted_job(
        job,
        job_store,
        _fake_execute_scripted(result_map, attempts=attempts),
    )
    return finished, attempts


async def _run_job_to_completed(client, job_store, root_url: str, execute, policy: dict | None = None):
    job_id = await _create_api_job(client, root_url, execute, policy)
    status = await _wait_status(job_store, job_id, {"completed", "failed"})
    assert status == "completed"
    return job_store.load(job_id)


async def _run_scripted_job(
    client, job_store, root_url: str, sequence: list[dict]
) -> tuple[Any, dict[str, int]]:
    attempts: dict[str, int] = {}
    job = await _run_job_to_completed(
        client,
        job_store,
        root_url,
        _fake_execute_scripted({root_url: sequence}, attempts=attempts),
    )
    return job, attempts


__all__ = [
    "_create_api_job",
    "_execution_payload",
    "_execution_policy",
    "_execution_urls",
    "_fake_execute",
    "_fake_execute_scripted",
    "_make_app",
    "_run_job_to_completed",
    "_run_persisted_job",
    "_run_scripted_job",
    "_run_scripted_persisted_job",
    "_seed_job",
    "_wait_status",
    "_wait_until",
    "make_job_store",
    "run_job_control_test",
]
